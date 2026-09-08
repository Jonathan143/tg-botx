"""按版本执行的增量迁移；旧版 schema_version=2 数据库从版本 3 继续升级。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import inspect, select, insert
from sqlalchemy.engine import Connection, Engine

from tg_botx.infrastructure.persistence.models import Base, SchemaVersion


def _add_columns(connection: Connection, table: str, definitions: dict[str, str]) -> set[str]:
    existing = {column["name"] for column in inspect(connection).get_columns(table)}
    added = set(definitions) - existing
    for column, ddl in definitions.items():
        if column in added:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return added


def _binding_roles(connection: Connection) -> None:
    for table in ("bot_binding_codes", "bot_bindings"):
        _add_columns(connection, table, {"role": "VARCHAR(20) DEFAULT 'user'"})


def _run_snapshots(connection: Connection) -> None:
    _add_columns(
        connection,
        "task_runs",
        {
            "run_kind": "TEXT DEFAULT 'published'",
            "workflow_version": "VARCHAR(30)",
            "workflow_version_id": "VARCHAR(36)",
            "workflow_json": "TEXT",
            "progress_json": "TEXT",
        },
    )


def _chat_avatars(connection: Connection) -> None:
    _add_columns(connection, "account_chats", {"avatar_photo_id": "BIGINT"})


def _published_schedule(connection: Connection) -> None:
    _add_columns(connection, "tasks", {"published_schedule_json": "TEXT"})


def _command_configuration(connection: Connection) -> None:
    added = _add_columns(
        connection,
        "bot_command_configs",
        {
            "menu_visible": "BOOLEAN DEFAULT TRUE",
            "sort_order": "INTEGER",
            "allowed_roles_json": "TEXT",
            "command_type": "VARCHAR(20) DEFAULT 'custom'",
            "executor_type": "VARCHAR(30) DEFAULT 'none'",
            "executor_config_json": "TEXT",
        },
    )
    clause = "" if "menu_visible" in added else " WHERE menu_visible IS NULL"
    connection.exec_driver_sql("UPDATE bot_command_configs SET menu_visible = enabled" + clause)
    connection.exec_driver_sql(
        "UPDATE bot_command_configs SET command_type = 'system' "
        "WHERE command IN ('start','help','bind','unbind','tasks','status','checkin')"
    )


MIGRATIONS: tuple[tuple[int, Callable[[Connection], None]], ...] = (
    (3, _binding_roles),
    (4, _run_snapshots),
    (5, _chat_avatars),
    (6, _published_schedule),
    (7, _command_configuration),
)


def migrate(engine: Engine) -> None:
    # New tables are created from metadata; existing tables are upgraded in order.
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(73482019)")
        elif connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(connection)
        applied = set(connection.scalars(select(SchemaVersion.version)))
        for version, upgrade in MIGRATIONS:
            if version not in applied:
                upgrade(connection)
                connection.execute(insert(SchemaVersion).values(version=version))
