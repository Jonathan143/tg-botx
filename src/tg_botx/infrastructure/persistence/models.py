from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tg_botx.core.time import utc_isoformat as utc_isoformat

PERMANENT_EXPIRY = datetime.max.replace(tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


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
            value = value.replace(tzinfo=UTC)
        value = value.astimezone(UTC)
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
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


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


class BotUserPoint(Base):
    """Points and daily check-in state for a Telegram user.

    Rows are retained after unbinding so a later re-bind does not erase a
    user's accumulated points.  ``last_checkin_date`` is stored as a UTC
    calendar date, matching the database's UTC persistence convention.
    """

    __tablename__ = "bot_user_points"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_checkin_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class BotSetting(Base):
    """Small persisted settings owned by the management Bot."""

    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


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
