from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    func,
    inspect,
    or_,
    select,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from tg_botx.core.time import utc_isoformat

PERMANENT_EXPIRY = datetime.max.replace(tzinfo=timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Store datetimes as UTC and always return timezone-aware values.

    SQLite drops the timezone component from ``DateTime(timezone=True)``
    columns.  Treating naive values as UTC on both sides keeps persisted data
    compatible while preventing naive/aware datetime comparisons in the
    scheduler.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        # SQLite has no timezone-aware datetime type, so preserve the existing
        # naive-UTC storage format.  PostgreSQL's TIMESTAMP WITH TIME ZONE
        # should receive an aware value so the server timezone cannot alter it.
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_name: Mapped[str] = mapped_column(String(100), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class AccountChat(Base):
    """A cached Telegram dialog belonging to an account.

    Telegram dialogs are refreshed from the account client by the admin API.
    Rows are retained when a dialog disappears from Telegram so a transient
    sync or an administrator's old task configuration cannot lose metadata;
    ``is_active`` controls whether the row is returned by the chat list API.
    """

    __tablename__ = "account_chats"
    __table_args__ = (
        UniqueConstraint("account_id", "chat_id", name="uq_account_chat_account_chat"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    chat_id: Mapped[str] = mapped_column(String(64))
    chat_type: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    has_avatar: Mapped[bool] = mapped_column(Boolean, default=False)
    # Telegram photo ids are stable for the lifetime of a photo and are used
    # as the cache-file version.  Keep this nullable for databases created by
    # older builds that only persisted ``has_avatar``.
    avatar_photo_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    target: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    schedule_type: Mapped[str] = mapped_column(String(20))
    fixed_time: Mapped[str | None] = mapped_column(String(8), nullable=True)
    random_start: Mapped[str | None] = mapped_column(String(8), nullable=True)
    random_end: Mapped[str | None] = mapped_column(String(8), nullable=True)
    config_json: Mapped[str] = mapped_column(Text)
    # The editable configuration is kept in ``config_json``.  This snapshot
    # is the schedule currently published for formal runs; it must not move
    # when an enabled task is edited until that draft is published.
    published_schedule_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

    @property
    def config(self) -> dict[str, Any]:
        return json.loads(self.config_json)


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("task_id", "version_number", name="uq_workflow_version_task_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    workflow_json: Mapped[str] = mapped_column(Text)
    release_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    published_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    @property
    def execution_definition(self) -> dict[str, Any]:
        return json.loads(self.workflow_json)

    @property
    def version(self) -> int:
        """Short alias used by API serializers and callers."""

        return self.version_number


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    planned_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="running")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_kind: Mapped[str] = mapped_column(String(20), default="published")
    workflow_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    workflow_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    workflow_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class AdminSession(Base):
    """Persisted administrator session metadata.

    Only the HMAC digest of the opaque cookie token is stored.  The token
    itself remains in the browser cookie and the CSRF token is derived from it
    by :class:`SessionManager`.
    """

    __tablename__ = "admin_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime())


class BotBindingCode(Base):
    """One-time code issued by the web/CLI administrator for Bot binding."""

    __tablename__ = "bot_binding_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    code_hint: Mapped[str] = mapped_column(String(8))
    role: Mapped[str] = mapped_column(String(20), default="user", server_default="user")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class BotBinding(Base):
    """A Telegram private user authorized to operate the management bot."""

    __tablename__ = "bot_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Historical rows are retained after unbinding, so this cannot be unique;
    # the active row is selected by ``is_active`` in the data-access methods.
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(20), default="user", server_default="user")
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bound_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    unbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class BotBindingBatch(Base):
    """Persistent idempotency record for generated binding-code batches."""

    __tablename__ = "bot_binding_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(20), default="user", server_default="user")
    quantity: Mapped[int] = mapped_column(Integer)
    ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    code_ids_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class BotCommandConfig(Base):
    """Administrator-configurable command menu entry for the management bot."""

    __tablename__ = "bot_command_configs"

    command: Mapped[str] = mapped_column(String(32), primary_key=True)
    description: Mapped[str] = mapped_column(String(256))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    menu_visible: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_type: Mapped[str] = mapped_column(
        String(20), default="custom", server_default="custom", nullable=False
    )
    executor_type: Mapped[str] = mapped_column(
        String(30), default="none", server_default="none", nullable=False
    )
    executor_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON array of identities allowed to invoke this command.  ``NULL`` is
    # retained for rows created before command-level authorization existed;
    # the management service supplies the appropriate default in that case.
    allowed_roles_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class BotAuditLog(Base):
    """Security-relevant management bot action without secret payloads."""

    __tablename__ = "bot_audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    actor_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(50), index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    task_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    result: Mapped[str] = mapped_column(String(30))
    update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, index=True)


class Database:
    def __init__(self, url: str):
        # SQLAlchemy's ``postgresql://`` shorthand defaults to psycopg2.  The
        # project ships psycopg 3, so make the driver explicit for either
        # PostgreSQL URL spelling.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://") :]

        database_url = make_url(url)
        engine_kwargs: dict[str, Any] = {}
        if database_url.get_backend_name() == "sqlite":
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        elif database_url.get_backend_name() == "postgresql":
            # Admin requests are long-lived enough to encounter stale pooled
            # connections (for example after a database failover).  Probe a
            # connection before handing it to SQLAlchemy and recycle idle
            # connections before common cloud load balancer timeouts.
            engine_kwargs.update(
                pool_pre_ping=True,
                pool_recycle=1_800,
                pool_timeout=10,
                connect_args={"connect_timeout": 10},
            )
        self.engine = create_engine(url, **engine_kwargs)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        # Binding role/idempotency schema migration for existing databases.
        for table, column, ddl in (
            ("bot_binding_codes", "role", "VARCHAR(20) DEFAULT 'user'"),
            ("bot_bindings", "role", "VARCHAR(20) DEFAULT 'user'"),
        ):
            columns = {item["name"] for item in inspect(self.engine).get_columns(table)}
            if column not in columns:
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # Keep the local development database usable after the destructive
        # version-model change.  New installations are created from metadata;
        # existing tables receive only the new nullable columns.
        task_run_columns = {item["name"] for item in inspect(self.engine).get_columns("task_runs")}
        missing = {
            "run_kind": "TEXT DEFAULT 'published'",
            "workflow_version": "VARCHAR(30)",
            "workflow_version_id": "VARCHAR(36)",
            "workflow_json": "TEXT",
            "progress_json": "TEXT",
        }
        additions = {name: ddl for name, ddl in missing.items() if name not in task_run_columns}
        if additions:
            with self.engine.begin() as connection:
                for name, ddl in additions.items():
                    connection.exec_driver_sql(f"ALTER TABLE task_runs ADD COLUMN {name} {ddl}")

        # ``account_chats`` was introduced after the initial admin release.
        # Add the nullable avatar version to an existing local database
        # without requiring a destructive migration.
        account_chat_columns = {
            item["name"] for item in inspect(self.engine).get_columns("account_chats")
        }
        if "avatar_photo_id" not in account_chat_columns:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE account_chats ADD COLUMN avatar_photo_id BIGINT"
                )

        task_columns = {item["name"] for item in inspect(self.engine).get_columns("tasks")}
        if "published_schedule_json" not in task_columns:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE tasks ADD COLUMN published_schedule_json TEXT"
                )

        command_columns = {
            item["name"] for item in inspect(self.engine).get_columns("bot_command_configs")
        }
        command_additions = {
            "menu_visible": "BOOLEAN DEFAULT TRUE",
            "sort_order": "INTEGER",
            "allowed_roles_json": "TEXT",
            "command_type": "VARCHAR(20) DEFAULT 'custom'",
            "executor_type": "VARCHAR(30) DEFAULT 'none'",
            "executor_config_json": "TEXT",
        }
        missing_command_columns = {
            name: ddl for name, ddl in command_additions.items() if name not in command_columns
        }
        menu_visible_migrated = "menu_visible" in missing_command_columns
        if missing_command_columns:
            with self.engine.begin() as connection:
                for name, ddl in missing_command_columns.items():
                    connection.exec_driver_sql(
                        f"ALTER TABLE bot_command_configs ADD COLUMN {name} {ddl}"
                    )
                # Existing built-in rows are protected system commands. Any
                # historical rows outside this allow-list remain custom.
        # Re-assert the protected type on every startup so databases migrated
        # by an earlier build cannot accidentally downgrade built-ins.
        with self.engine.begin() as connection:
            if menu_visible_migrated:
                connection.exec_driver_sql("UPDATE bot_command_configs SET menu_visible = enabled")
            else:
                connection.exec_driver_sql(
                    "UPDATE bot_command_configs SET menu_visible = enabled "
                    "WHERE menu_visible IS NULL"
                )
            connection.exec_driver_sql(
                "UPDATE bot_command_configs SET command_type = 'system' "
                "WHERE command IN ('start','help','bind','unbind','tasks','status')"
            )
        with self.session() as session:
            if session.get(SchemaVersion, 2) is None:
                session.add(SchemaVersion(version=2))
                session.commit()

    def session(self) -> Session:
        return self.Session()

    def get_account(self, name: str = "default") -> Account | None:
        with self.session() as session:
            return session.scalar(select(Account).where(Account.name == name))

    def get_account_by_id(self, account_id: str) -> Account | None:
        with self.session() as session:
            return session.get(Account, account_id)

    def list_accounts(self) -> list[Account]:
        with self.session() as session:
            return list(session.scalars(select(Account).order_by(Account.created_at, Account.name)))

    def get_task(self, task_id_or_name: str) -> Task | None:
        with self.session() as session:
            task = session.get(Task, task_id_or_name)
            if task:
                return task
            return session.scalar(
                select(Task).where(Task.name == task_id_or_name, Task.archived.is_(False))
            )

    def get_task_any(self, task_id_or_name: str) -> Task | None:
        """Return a task by ID or name, including archived tasks."""

        with self.session() as session:
            task = session.get(Task, task_id_or_name)
            if task:
                return task
            return session.scalar(select(Task).where(Task.name == task_id_or_name))

    def list_tasks(self, include_archived: bool = False) -> list[Task]:
        with self.session() as session:
            query = select(Task).order_by(Task.created_at)
            if not include_archived:
                query = query.where(Task.archived.is_(False))
            return list(session.scalars(query))

    def list_tasks_page(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        include_archived: bool = False,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> tuple[list[Task], int]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        filters: list[Any] = []
        if not include_archived:
            filters.append(Task.archived.is_(False))
        if enabled is not None:
            filters.append(Task.enabled.is_(enabled))
        if search and (term := search.strip()):
            pattern = f"%{term}%"
            filters.append(or_(Task.name.ilike(pattern), Task.target.ilike(pattern)))

        with self.session() as session:
            total = session.scalar(select(func.count(Task.id)).where(*filters)) or 0
            query = (
                select(Task)
                .where(*filters)
                .order_by(Task.created_at.desc(), Task.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return list(session.scalars(query)), int(total)

    def save_account(self, account: Account) -> Account:
        with self.session() as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            return account

    def list_account_chats(
        self,
        account_id: str,
        *,
        chat_type: str = "all",
        query: str | None = None,
        limit: int = 200,
    ) -> list[AccountChat]:
        """Return active cached chats with optional type/text filtering."""

        limit = min(max(limit, 1), 500)
        filters: list[Any] = [
            AccountChat.account_id == account_id,
            AccountChat.is_active.is_(True),
        ]
        if chat_type != "all":
            filters.append(AccountChat.chat_type == chat_type)
        if query and (term := query.strip()):
            pattern = f"%{term}%"
            filters.append(
                or_(
                    AccountChat.chat_id.ilike(pattern),
                    AccountChat.title.ilike(pattern),
                    AccountChat.username.ilike(pattern),
                )
            )
        with self.session() as session:
            statement = (
                select(AccountChat)
                .where(*filters)
                .order_by(AccountChat.sort_order, AccountChat.title, AccountChat.chat_id)
                .limit(limit)
            )
            return list(session.scalars(statement))

    def get_account_chat(self, account_id: str, chat_id: str) -> AccountChat | None:
        """Return one cached chat row, including inactive rows."""

        with self.session() as session:
            return session.scalar(
                select(AccountChat).where(
                    AccountChat.account_id == account_id,
                    AccountChat.chat_id == chat_id,
                )
            )

    def update_account_chat_avatar(
        self, account_id: str, chat_id: str, photo_id: int | None
    ) -> None:
        """Persist the photo version observed while downloading an avatar."""

        with self.session() as session:
            row = session.scalar(
                select(AccountChat).where(
                    AccountChat.account_id == account_id,
                    AccountChat.chat_id == chat_id,
                )
            )
            if row is not None:
                row.has_avatar = photo_id is not None
                row.avatar_photo_id = photo_id
                row.updated_at = utc_now()
                session.commit()

    def upsert_account_chats(
        self,
        account_id: str,
        chats: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Incrementally persist a freshly pulled dialog snapshot.

        Existing rows are updated only when metadata changed.  Dialogs absent
        from the snapshot are marked inactive instead of being deleted, which
        keeps old task references and metadata recoverable.
        """

        with self.session() as session:
            existing_rows = list(
                session.scalars(select(AccountChat).where(AccountChat.account_id == account_id))
            )
            existing = {row.chat_id: row for row in existing_rows}
            seen: set[str] = set()
            added = 0
            updated = 0
            for sort_order, payload in enumerate(chats):
                chat_id = str(payload["chat_id"])
                if chat_id in seen:
                    continue
                seen.add(chat_id)
                values: dict[str, Any] = {
                    "chat_type": str(payload["chat_type"]),
                    "title": str(payload.get("title") or chat_id)[:255],
                    "username": payload.get("username"),
                    "has_avatar": bool(payload.get("has_avatar", False)),
                    "sort_order": sort_order,
                }
                # Keep the version captured by a newer build when an older
                # caller submits a payload that does not know this field yet.
                if "avatar_photo_id" in payload:
                    values["avatar_photo_id"] = payload.get("avatar_photo_id")
                row = existing.get(chat_id)
                if row is None:
                    session.add(
                        AccountChat(
                            account_id=account_id,
                            chat_id=chat_id,
                            is_active=True,
                            **values,
                        )
                    )
                    added += 1
                    continue
                changed = any(getattr(row, key) != value for key, value in values.items())
                if not row.is_active:
                    changed = True
                if changed:
                    for key, value in values.items():
                        setattr(row, key, value)
                    row.is_active = True
                    row.updated_at = utc_now()
                    updated += 1

            removed = 0
            for row in existing_rows:
                if row.chat_id not in seen and row.is_active:
                    row.is_active = False
                    row.updated_at = utc_now()
                    removed += 1
            session.commit()
            return {
                "added": added,
                "updated": updated,
                "removed": removed,
                "total": len(seen),
            }

    def save_task(self, task: Task) -> Task:
        with self.session() as session:
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    def save_admin_session(
        self, token_hash: str, expires_at: datetime, last_seen_at: datetime
    ) -> None:
        with self.session() as session:
            item = session.get(AdminSession, token_hash)
            if item is None:
                item = AdminSession(token_hash=token_hash)
                session.add(item)
            item.expires_at = expires_at
            item.last_seen_at = last_seen_at
            session.commit()

    def get_admin_session(self, token_hash: str) -> AdminSession | None:
        with self.session() as session:
            return session.get(AdminSession, token_hash)

    def delete_admin_session(self, token_hash: str) -> None:
        with self.session() as session:
            item = session.get(AdminSession, token_hash)
            if item is not None:
                session.delete(item)
                session.commit()

    def delete_expired_admin_sessions(self, now: datetime) -> None:
        with self.session() as session:
            session.query(AdminSession).filter(AdminSession.expires_at <= now).delete(
                synchronize_session=False
            )
            session.commit()

    def delete_all_admin_sessions(self) -> None:
        with self.session() as session:
            session.query(AdminSession).delete(synchronize_session=False)
            session.commit()

    def create_bot_binding_code(
        self, code_hash: str, code_hint: str, expires_at: datetime | None, role: str = "user"
    ) -> BotBindingCode:
        with self.session() as session:
            stored_expiry = expires_at
            if stored_expiry is None and session.bind is not None and session.bind.dialect.name == "sqlite":
                stored_expiry = PERMANENT_EXPIRY
            item = BotBindingCode(code_hash=code_hash, code_hint=code_hint, expires_at=stored_expiry, role=role)
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def create_bot_binding_codes(
        self, items: list[tuple[str, str, datetime | None, str]], *,
        idempotency_key: str | None = None, request_hash: str | None = None,
        ttl_days: int | None = None,
    ) -> tuple[BotBindingBatch | None, list[BotBindingCode]]:
        with self.session() as session:
            if idempotency_key:
                existing = session.scalar(select(BotBindingBatch).where(BotBindingBatch.idempotency_key == idempotency_key))
                if existing:
                    if existing.request_hash != request_hash:
                        raise ValueError("IDEMPOTENCY_CONFLICT")
                    ids = json.loads(existing.code_ids_json)
                    codes = list(session.scalars(select(BotBindingCode).where(BotBindingCode.id.in_(ids))))
                    return existing, sorted(codes, key=lambda item: ids.index(item.id))
            codes = []
            for code_hash, hint, expires_at, role in items:
                # Legacy SQLite schemas declared expires_at NOT NULL; use a
                # far-future sentinel there while exposing permanent as null
                # at the API boundary.
                stored_expiry = expires_at
                if stored_expiry is None and session.bind is not None and session.bind.dialect.name == "sqlite":
                    stored_expiry = PERMANENT_EXPIRY
                item = BotBindingCode(code_hash=code_hash, code_hint=hint, expires_at=stored_expiry, role=role)
                session.add(item)
                codes.append(item)
            session.flush()
            batch = None
            if idempotency_key:
                batch = BotBindingBatch(
                    idempotency_key=idempotency_key, request_hash=request_hash or "",
                    role=items[0][3] if items else "user", quantity=len(items), ttl_days=ttl_days,
                    code_ids_json=json.dumps([item.id for item in codes]),
                )
                session.add(batch)
            session.commit()
            for item in codes:
                session.refresh(item)
            if batch:
                session.refresh(batch)
            return batch, codes

    def get_bot_binding_batch(self, idempotency_key: str) -> BotBindingBatch | None:
        with self.session() as session:
            return session.scalar(select(BotBindingBatch).where(BotBindingBatch.idempotency_key == idempotency_key))

    def get_bot_binding_code(self, code_id: str) -> BotBindingCode | None:
        with self.session() as session:
            return session.get(BotBindingCode, code_id)

    def list_bot_binding_codes(self) -> list[BotBindingCode]:
        with self.session() as session:
            return list(
                session.scalars(select(BotBindingCode).order_by(BotBindingCode.created_at.desc()))
            )

    def list_bot_binding_codes_page(self, *, page: int, page_size: int) -> tuple[list[BotBindingCode], int]:
        with self.session() as session:
            base = select(BotBindingCode).order_by(BotBindingCode.created_at.desc())
            items = list(session.scalars(base.offset((page - 1) * page_size).limit(page_size)))
            total = session.scalar(select(func.count()).select_from(BotBindingCode)) or 0
            return items, total

    def revoke_bot_binding_code(self, code_id: str) -> bool:
        with self.session() as session:
            item = session.get(BotBindingCode, code_id)
            if item is None or item.used_at is not None or item.revoked_at is not None:
                return False
            item.revoked_at = utc_now()
            session.commit()
            return True

    def consume_bot_binding_code(
        self,
        code_hash: str,
        *,
        user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> BotBinding | None:
        now = utc_now()
        with self.session() as session:
            item = session.scalar(
                select(BotBindingCode).where(
                    BotBindingCode.code_hash == code_hash,
                    BotBindingCode.used_at.is_(None),
                    BotBindingCode.revoked_at.is_(None),
                    or_(BotBindingCode.expires_at.is_(None), BotBindingCode.expires_at > now),
                )
            )
            if item is None:
                return None
            previous = session.scalar(select(BotBinding).where(BotBinding.user_id == user_id, BotBinding.is_active.is_(True)))
            if previous is not None:
                return None
            item.used_at = now
            binding = BotBinding(
                user_id=user_id,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                role=item.role or "user",
                bound_at=now,
                is_active=True,
            )
            session.add(binding)
            session.commit()
            session.refresh(binding)
            return binding

    def get_bot_binding(self, user_id: int, *, active_only: bool = True) -> BotBinding | None:
        with self.session() as session:
            filters: list[Any] = [BotBinding.user_id == user_id]
            if active_only:
                filters.append(BotBinding.is_active.is_(True))
            return session.scalar(select(BotBinding).where(*filters))

    def list_bot_bindings(self, *, active_only: bool = True) -> list[BotBinding]:
        with self.session() as session:
            query = select(BotBinding).order_by(BotBinding.bound_at.desc())
            if active_only:
                query = query.where(BotBinding.is_active.is_(True))
            return list(session.scalars(query))

    def list_bot_bindings_page(self, *, page: int, page_size: int, active_only: bool = True) -> tuple[list[BotBinding], int]:
        with self.session() as session:
            query = select(BotBinding)
            count_query = select(func.count()).select_from(BotBinding)
            if active_only:
                query = query.where(BotBinding.is_active.is_(True))
                count_query = count_query.where(BotBinding.is_active.is_(True))
            query = query.order_by(BotBinding.bound_at.desc())
            items = list(session.scalars(query.offset((page - 1) * page_size).limit(page_size)))
            total = session.scalar(count_query) or 0
            return items, total

    def revoke_bot_binding(self, binding_id: str) -> bool:
        with self.session() as session:
            item = session.get(BotBinding, binding_id)
            if item is None or not item.is_active:
                return False
            item.is_active = False
            item.unbound_at = utc_now()
            session.commit()
            return True

    def list_bot_command_configs(self) -> list[BotCommandConfig]:
        with self.session() as session:
            return list(
                session.scalars(select(BotCommandConfig).order_by(BotCommandConfig.command))
            )

    def upsert_bot_command_config(
        self,
        command: str,
        description: str,
        enabled: bool,
        allowed_roles_json: str | None = None,
        *,
        menu_visible: bool | None = None,
        command_type: str | None = None,
        executor_type: str | None = None,
        executor_config_json: str | None = None,
    ) -> BotCommandConfig:
        with self.session() as session:
            item = session.get(BotCommandConfig, command)
            if item is None:
                item = BotCommandConfig(command=command)
                session.add(item)
            item.description = description
            item.enabled = enabled
            if menu_visible is not None:
                item.menu_visible = menu_visible
            if allowed_roles_json is not None:
                item.allowed_roles_json = allowed_roles_json
            if command_type is not None:
                item.command_type = command_type
            if executor_type is not None:
                item.executor_type = executor_type
            if executor_config_json is not None:
                item.executor_config_json = executor_config_json
            item.updated_at = utc_now()
            session.commit()
            session.refresh(item)
            return item

    def rename_bot_command_config(self, command: str, new_command: str) -> BotCommandConfig | None:
        with self.session() as session:
            item = session.get(BotCommandConfig, command)
            if item is None:
                return None
            item.command = new_command
            item.updated_at = utc_now()
            session.commit()
            session.refresh(item)
            return item

    def set_bot_command_order(self, command: str, sort_order: int) -> bool:
        with self.session() as session:
            item = session.get(BotCommandConfig, command)
            if item is None:
                return False
            item.sort_order = sort_order
            item.updated_at = utc_now()
            session.commit()
            return True

    def delete_bot_command_config(self, command: str) -> bool:
        """Remove a persisted management-bot command configuration."""
        with self.session() as session:
            item = session.get(BotCommandConfig, command)
            if item is None:
                return False
            session.delete(item)
            session.commit()
            return True

    def add_bot_audit_log(self, item: BotAuditLog) -> BotAuditLog:
        with self.session() as session:
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def bot_audit_logs_since(self, since: datetime) -> list[BotAuditLog]:
        with self.session() as session:
            return list(
                session.scalars(
                    select(BotAuditLog)
                    .where(BotAuditLog.created_at >= since)
                    .order_by(BotAuditLog.created_at.desc())
                )
            )

    def update_task(self, task_id: str, **values) -> Task:
        with self.session() as session:
            task = session.get(Task, task_id)
            if not task:
                raise KeyError(task_id)
            for key, value in values.items():
                setattr(task, key, value)
            task.updated_at = utc_now()
            session.commit()
            session.refresh(task)
            return task

    def add_run(self, run: TaskRun) -> TaskRun:
        with self.session() as session:
            session.add(run)
            session.commit()
            session.refresh(run)
            return run

    def publish_workflow(
        self,
        task_id: str,
        execution_definition: dict[str, Any],
        *,
        release_note: str | None = None,
        published_by: str | None = None,
    ) -> WorkflowVersion:
        with self.session() as session:
            latest = session.scalar(
                select(WorkflowVersion.version_number)
                .where(WorkflowVersion.task_id == task_id)
                .order_by(WorkflowVersion.version_number.desc())
                .limit(1)
            )
            version = WorkflowVersion(
                task_id=task_id,
                version_number=(latest or 0) + 1,
                workflow_json=json.dumps(execution_definition, ensure_ascii=False),
                release_note=release_note.strip() if release_note else None,
                published_by=published_by,
            )
            session.add(version)
            session.commit()
            session.refresh(version)
            return version

    def get_workflow_version(self, version_id: str) -> WorkflowVersion | None:
        with self.session() as session:
            return session.get(WorkflowVersion, version_id)

    def get_latest_workflow_version(self, task_id: str) -> WorkflowVersion | None:
        with self.session() as session:
            return session.scalar(
                select(WorkflowVersion)
                .where(WorkflowVersion.task_id == task_id)
                .order_by(WorkflowVersion.version_number.desc())
                .limit(1)
            )

    def list_workflow_versions(self, task_id: str) -> list[WorkflowVersion]:
        with self.session() as session:
            return list(
                session.scalars(
                    select(WorkflowVersion)
                    .where(WorkflowVersion.task_id == task_id)
                    .order_by(WorkflowVersion.version_number.desc())
                )
            )

    def update_run(self, run_id: str, **values) -> TaskRun:
        with self.session() as session:
            run = session.get(TaskRun, run_id)
            if not run:
                raise KeyError(run_id)
            for key, value in values.items():
                setattr(run, key, value)
            session.commit()
            session.refresh(run)
            return run

    def get_run(self, run_id: str) -> TaskRun | None:
        with self.session() as session:
            return session.get(TaskRun, run_id)

    def has_running_run(self, task_id: str) -> bool:
        with self.session() as session:
            query = (
                select(TaskRun.id)
                .where(TaskRun.task_id == task_id, TaskRun.status == "running")
                .limit(1)
            )
            return session.scalar(query) is not None

    def task_history(self, task_id: str, limit: int = 20) -> list[TaskRun]:
        with self.session() as session:
            query = (
                select(TaskRun)
                .where(TaskRun.task_id == task_id)
                .order_by(TaskRun.started_at.desc())
                .limit(limit)
            )
            return list(session.scalars(query))

    def list_runs(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        task_id: str | None = None,
        status: str | None = None,
        started_from: datetime | None = None,
        started_to: datetime | None = None,
    ) -> tuple[list[TaskRun], int]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        filters = []
        if task_id:
            filters.append(TaskRun.task_id == task_id)
        if status:
            filters.append(TaskRun.status == status)
        if started_from:
            filters.append(TaskRun.started_at >= started_from)
        if started_to:
            filters.append(TaskRun.started_at <= started_to)

        with self.session() as session:
            total = session.scalar(select(func.count(TaskRun.id)).where(*filters)) or 0
            query = (
                select(TaskRun)
                .where(*filters)
                .order_by(TaskRun.started_at.desc(), TaskRun.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return list(session.scalars(query)), int(total)

    def account_task_summary(self, account_id: str) -> dict[str, int]:
        with self.session() as session:
            rows = session.execute(
                select(Task.enabled, Task.archived, func.count(Task.id))
                .where(Task.account_id == account_id)
                .group_by(Task.enabled, Task.archived)
            )
            summary = {"total": 0, "enabled": 0, "archived": 0}
            for enabled, archived, count in rows:
                value = int(count)
                summary["total"] += value
                if enabled and not archived:
                    summary["enabled"] += value
                if archived:
                    summary["archived"] += value
            return summary

    def dashboard_stats(self, since: datetime) -> dict[str, Any]:
        """Return compact aggregate counters used by the administration dashboard."""

        with self.session() as session:
            task_rows = session.execute(
                select(Task.enabled, Task.archived, func.count(Task.id)).group_by(
                    Task.enabled, Task.archived
                )
            )
            result: dict[str, Any] = {
                "tasks_total": 0,
                "tasks_enabled": 0,
                "tasks_archived": 0,
                "runs_total": 0,
                "runs_success": 0,
                "runs_failed": 0,
                "runs_canceled": 0,
                "runs_skipped": 0,
                "runs_running": 0,
                "runStatusCounts": {},
            }
            for enabled, archived, count in task_rows:
                value = int(count)
                result["tasks_total"] += value
                if enabled and not archived:
                    result["tasks_enabled"] += value
                if archived:
                    result["tasks_archived"] += value

            run_rows = session.execute(
                select(TaskRun.status, func.count(TaskRun.id))
                .where(TaskRun.started_at >= since)
                .group_by(TaskRun.status)
            )
            for status, count in run_rows:
                value = int(count)
                result["runs_total"] += value
                result["runStatusCounts"][status] = value
                key = f"runs_{status}"
                if key in result:
                    result[key] += value
            return result

    def dashboard_run_events(self, since: datetime) -> list[tuple[datetime, str]]:
        """Return the minimal run data needed to build UTC dashboard buckets."""

        with self.session() as session:
            rows = session.execute(
                select(TaskRun.started_at, TaskRun.status)
                .where(TaskRun.started_at >= since)
                .order_by(TaskRun.started_at)
            )
            return [(started_at, status) for started_at, status in rows]

    def upcoming_tasks(self, limit: int = 10) -> list[Task]:
        with self.session() as session:
            query = (
                select(Task)
                .where(
                    Task.enabled.is_(True),
                    Task.archived.is_(False),
                    Task.next_run_at.is_not(None),
                )
                .order_by(Task.next_run_at, Task.id)
                .limit(min(max(limit, 1), 100))
            )
            return list(session.scalars(query))
