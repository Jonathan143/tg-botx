from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    create_engine,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from tg_botx.core.time import utc_isoformat as utc_isoformat
from tg_botx.infrastructure.persistence.models import (
    PERMANENT_EXPIRY as PERMANENT_EXPIRY,
)
from tg_botx.infrastructure.persistence.models import (
    Account as Account,
)
from tg_botx.infrastructure.persistence.models import (
    AccountChat as AccountChat,
)
from tg_botx.infrastructure.persistence.models import (
    AdminSession as AdminSession,
)
from tg_botx.infrastructure.persistence.models import (
    Base as Base,
)
from tg_botx.infrastructure.persistence.models import (
    BotAuditLog as BotAuditLog,
)
from tg_botx.infrastructure.persistence.models import (
    BotBinding as BotBinding,
)
from tg_botx.infrastructure.persistence.models import (
    BotBindingBatch as BotBindingBatch,
)
from tg_botx.infrastructure.persistence.models import (
    BotBindingCode as BotBindingCode,
)
from tg_botx.infrastructure.persistence.models import (
    BotCommandConfig as BotCommandConfig,
)
from tg_botx.infrastructure.persistence.models import (
    BotSetting as BotSetting,
)
from tg_botx.infrastructure.persistence.models import (
    BotUserPoint as BotUserPoint,
)
from tg_botx.infrastructure.persistence.models import (
    SchemaVersion as SchemaVersion,
)
from tg_botx.infrastructure.persistence.models import (
    Task as Task,
)
from tg_botx.infrastructure.persistence.models import (
    TaskRun as TaskRun,
)
from tg_botx.infrastructure.persistence.models import (
    UTCDateTime as UTCDateTime,
)
from tg_botx.infrastructure.persistence.models import (
    WorkflowVersion as WorkflowVersion,
)
from tg_botx.infrastructure.persistence.models import (
    utc_now as utc_now,
)
from tg_botx.infrastructure.persistence.repositories.accounts import AccountsRepository
from tg_botx.infrastructure.persistence.repositories.bot import BotRepository
from tg_botx.infrastructure.persistence.repositories.dashboard import DashboardRepository
from tg_botx.infrastructure.persistence.repositories.runs import RunsRepository
from tg_botx.infrastructure.persistence.repositories.sessions import SessionsRepository
from tg_botx.infrastructure.persistence.repositories.tasks import TasksRepository


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
        self.accounts = AccountsRepository(self.session)
        self.tasks = TasksRepository(self.session)
        self.runs = RunsRepository(self.session)
        self.bot = BotRepository(self.session)
        self.sessions = SessionsRepository(self.session)
        self.dashboard = DashboardRepository(self.session)

    def create_all(self) -> None:
        from tg_botx.infrastructure.persistence.migrations import migrate

        migrate(self.engine)

    def session(self) -> Session:
        return self.Session()

    def get_account(self, name: str = "default") -> Account | None:
        return self.accounts.get_account(name)

    def get_account_by_id(self, account_id: str) -> Account | None:
        return self.accounts.get_account_by_id(account_id)

    def list_accounts(self) -> list[Account]:
        return self.accounts.list_accounts()

    def get_task(self, task_id_or_name: str) -> Task | None:
        return self.tasks.get_task(task_id_or_name)

    def get_task_any(self, task_id_or_name: str) -> Task | None:
        return self.tasks.get_task_any(task_id_or_name)

    def list_tasks(self, include_archived: bool = False) -> list[Task]:
        return self.tasks.list_tasks(include_archived)

    def list_tasks_page(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        include_archived: bool = False,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> tuple[list[Task], int]:
        return self.tasks.list_tasks_page(
            page=page,
            page_size=page_size,
            include_archived=include_archived,
            enabled=enabled,
            search=search,
        )

    def save_account(self, account: Account) -> Account:
        return self.accounts.save_account(account)

    def activate_account(self, name: str, phone: str | None) -> Account:
        return self.accounts.activate_account(name, phone)

    def deactivate_account(self, account_id: str) -> None:
        return self.accounts.deactivate_account(account_id)

    def list_account_chats(
        self, account_id: str, *, chat_type: str = "all", query: str | None = None, limit: int = 200
    ) -> list[AccountChat]:
        return self.accounts.list_account_chats(
            account_id, chat_type=chat_type, query=query, limit=limit
        )

    def get_account_chat(self, account_id: str, chat_id: str) -> AccountChat | None:
        return self.accounts.get_account_chat(account_id, chat_id)

    def update_account_chat_avatar(
        self, account_id: str, chat_id: str, photo_id: int | None
    ) -> None:
        return self.accounts.update_account_chat_avatar(account_id, chat_id, photo_id)

    def upsert_account_chats(self, account_id: str, chats: list[dict[str, Any]]) -> dict[str, int]:
        return self.accounts.upsert_account_chats(account_id, chats)

    def save_task(self, task: Task) -> Task:
        return self.tasks.save_task(task)

    def save_admin_session(
        self, token_hash: str, expires_at: datetime, last_seen_at: datetime
    ) -> None:
        return self.sessions.save_admin_session(token_hash, expires_at, last_seen_at)

    def get_admin_session(self, token_hash: str) -> AdminSession | None:
        return self.sessions.get_admin_session(token_hash)

    def delete_admin_session(self, token_hash: str) -> None:
        return self.sessions.delete_admin_session(token_hash)

    def delete_expired_admin_sessions(self, now: datetime) -> None:
        return self.sessions.delete_expired_admin_sessions(now)

    def delete_all_admin_sessions(self) -> None:
        return self.sessions.delete_all_admin_sessions()

    def create_bot_binding_code(
        self, code_hash: str, code_hint: str, expires_at: datetime | None, role: str = "user"
    ) -> BotBindingCode:
        return self.bot.create_bot_binding_code(code_hash, code_hint, expires_at, role)

    def create_bot_binding_codes(
        self,
        items: list[tuple[str, str, datetime | None, str]],
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        ttl_days: int | None = None,
    ) -> tuple[BotBindingBatch | None, list[BotBindingCode]]:
        return self.bot.create_bot_binding_codes(
            items, idempotency_key=idempotency_key, request_hash=request_hash, ttl_days=ttl_days
        )

    def get_bot_binding_batch(self, idempotency_key: str) -> BotBindingBatch | None:
        return self.bot.get_bot_binding_batch(idempotency_key)

    def get_bot_binding_code(self, code_id: str) -> BotBindingCode | None:
        return self.bot.get_bot_binding_code(code_id)

    def list_bot_binding_codes(self) -> list[BotBindingCode]:
        return self.bot.list_bot_binding_codes()

    def list_bot_binding_codes_page(
        self, *, page: int, page_size: int
    ) -> tuple[list[BotBindingCode], int]:
        return self.bot.list_bot_binding_codes_page(page=page, page_size=page_size)

    def revoke_bot_binding_code(self, code_id: str) -> bool:
        return self.bot.revoke_bot_binding_code(code_id)

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
        return self.bot.consume_bot_binding_code(
            code_hash,
            user_id=user_id,
            chat_id=chat_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )

    def get_bot_binding(self, user_id: int, *, active_only: bool = True) -> BotBinding | None:
        return self.bot.get_bot_binding(user_id, active_only=active_only)

    def list_bot_bindings(self, *, active_only: bool = True) -> list[BotBinding]:
        return self.bot.list_bot_bindings(active_only=active_only)

    def list_bot_bindings_page(
        self, *, page: int, page_size: int, active_only: bool = True
    ) -> tuple[list[BotBinding], int]:
        return self.bot.list_bot_bindings_page(
            page=page, page_size=page_size, active_only=active_only
        )

    def revoke_bot_binding(self, binding_id: str) -> bool:
        return self.bot.revoke_bot_binding(binding_id)

    def get_bot_user_points(self, user_id: int) -> BotUserPoint | None:
        return self.bot.get_bot_user_points(user_id)

    def checkin_bot_user(
        self, user_id: int, chat_id: int, amount_min: int, amount_max: int
    ) -> tuple[str, int, int]:
        return self.bot.checkin_bot_user(user_id, chat_id, amount_min, amount_max)

    def get_bot_setting(self, key: str) -> str | None:
        return self.bot.get_bot_setting(key)

    def set_bot_setting(self, key: str, value: str) -> BotSetting:
        return self.bot.set_bot_setting(key, value)

    def list_bot_command_configs(self) -> list[BotCommandConfig]:
        return self.bot.list_bot_command_configs()

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
        return self.bot.upsert_bot_command_config(
            command,
            description,
            enabled,
            allowed_roles_json,
            menu_visible=menu_visible,
            command_type=command_type,
            executor_type=executor_type,
            executor_config_json=executor_config_json,
        )

    def rename_bot_command_config(self, command: str, new_command: str) -> BotCommandConfig | None:
        return self.bot.rename_bot_command_config(command, new_command)

    def set_bot_command_order(self, command: str, sort_order: int) -> bool:
        return self.bot.set_bot_command_order(command, sort_order)

    def delete_bot_command_config(self, command: str) -> bool:
        return self.bot.delete_bot_command_config(command)

    def add_bot_audit_log(self, item: BotAuditLog) -> BotAuditLog:
        return self.bot.add_bot_audit_log(item)

    def bot_audit_logs_since(self, since: datetime) -> list[BotAuditLog]:
        return self.bot.bot_audit_logs_since(since)

    def update_task(self, task_id: str, **values) -> Task:
        return self.tasks.update_task(task_id, **values)

    def add_run(self, run: TaskRun) -> TaskRun:
        return self.runs.add_run(run)

    def publish_workflow(
        self,
        task_id: str,
        execution_definition: dict[str, Any],
        *,
        release_note: str | None = None,
        published_by: str | None = None,
        task_values: dict[str, Any] | None = None,
    ) -> WorkflowVersion:
        return self.tasks.publish_workflow(
            task_id,
            execution_definition,
            release_note=release_note,
            published_by=published_by,
            task_values=task_values,
        )

    def get_workflow_version(self, version_id: str) -> WorkflowVersion | None:
        return self.tasks.get_workflow_version(version_id)

    def get_latest_workflow_version(self, task_id: str) -> WorkflowVersion | None:
        return self.tasks.get_latest_workflow_version(task_id)

    def list_workflow_versions(self, task_id: str) -> list[WorkflowVersion]:
        return self.tasks.list_workflow_versions(task_id)

    def update_run(self, run_id: str, **values) -> TaskRun:
        return self.runs.update_run(run_id, **values)

    def get_run(self, run_id: str) -> TaskRun | None:
        return self.runs.get_run(run_id)

    def has_running_run(self, task_id: str) -> bool:
        return self.runs.has_running_run(task_id)

    def task_history(self, task_id: str, limit: int = 20) -> list[TaskRun]:
        return self.runs.task_history(task_id, limit)

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
        return self.runs.list_runs(
            page=page,
            page_size=page_size,
            task_id=task_id,
            status=status,
            started_from=started_from,
            started_to=started_to,
        )

    def account_task_summary(self, account_id: str) -> dict[str, int]:
        return self.accounts.account_task_summary(account_id)

    def dashboard_stats(self, since: datetime) -> dict[str, Any]:
        return self.dashboard.dashboard_stats(since)

    def dashboard_run_events(self, since: datetime) -> list[tuple[datetime, str]]:
        return self.dashboard.dashboard_run_events(since)

    def upcoming_tasks(self, limit: int = 10) -> list[Task]:
        return self.dashboard.upcoming_tasks(limit)
