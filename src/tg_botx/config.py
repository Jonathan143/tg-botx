import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TG_BOT_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    api_id: int | None = Field(default=None)
    api_hash: str | None = Field(default=None)
    data_dir: Path = Field(default=Path("./data"))
    database: Literal["sqlite", "postgresql"] = Field(default="sqlite")
    # A complete SQLAlchemy URL is required when PostgreSQL is selected.  The
    # aliases also accept the conventional unprefixed DATABASE_URL variable.
    database_url_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TG_BOT_DATABASE_URL", "DATABASE_URL"),
    )
    admin_chat_ids: str = Field(default="")
    notification_bot_token: SecretStr | None = Field(default=None)
    # Timezone used when formatting notification timestamps that are not tied
    # to a task schedule (service lifecycle and fatal-error events).
    notification_timezone: str = Field(default="Asia/Shanghai")
    # Service start/stop messages are noisy during local development (especially
    # when ``serve --reload`` restarts the worker).  Keep them independently
    # configurable so task result and error notifications remain available.
    service_lifecycle_notifications_enabled: bool = Field(default=True)
    log_level: str = Field(default="INFO")
    log_file: str = Field(default="tg-bot.log")
    log_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    log_backup_count: int = Field(default=5, ge=0)
    admin_key: SecretStr | None = Field(default=None)
    # Separate Telegram Bot API credential for the interactive management bot.
    # ``admin_key`` remains exclusively the web/API administrator secret.
    admin_bot_token: SecretStr | None = Field(default=None)
    # The management bot can receive updates through Telegram long polling or
    # through the FastAPI webhook endpoint exposed by this service.
    bot_transport: Literal["long_polling", "webhook"] = Field(default="long_polling")
    bot_webhook_url: str | None = Field(default=None)
    bot_webhook_secret: SecretStr | None = Field(default=None)
    admin_origin: str | None = Field(default=None)
    admin_session_days: int = Field(default=30, ge=1, le=365)
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, ge=1, le=65535)
    transport_key_rotation_hours: int = Field(default=24, ge=1, le=168)
    trusted_proxies: str = Field(default="")
    # Feature switches keep future Telegram bot capabilities opt-in while the
    # existing check-in service remains the default deployment.
    bot_enabled: bool = Field(default=False)
    channel_notifications_enabled: bool = Field(default=False)
    group_monitor_enabled: bool = Field(default=False)
    group_monitor_history_limit: int = Field(default=500, ge=1, le=100_000)

    @property
    def enabled_features(self) -> frozenset[str]:
        """Return explicitly enabled optional capabilities."""

        values = {
            "bot": self.bot_enabled,
            "channel_notifications": self.channel_notifications_enabled,
            "group_monitor": self.group_monitor_enabled,
        }
        return frozenset(name for name, enabled in values.items() if enabled)

    @field_validator("database", mode="before")
    @classmethod
    def normalize_database(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized in {"postgres", "postgresql"}:
            return "postgresql"
        if normalized in {"sqlite", "sqlite3"}:
            return "sqlite"
        raise ValueError("TG_BOT_DATABASE 仅支持 sqlite 或 postgresql")

    @field_validator("bot_transport", mode="before")
    @classmethod
    def normalize_bot_transport(cls, value: str) -> str:
        normalized = str(value).strip().lower().replace("-", "_")
        if normalized in {"polling", "long_polling"}:
            return "long_polling"
        if normalized == "webhook":
            return "webhook"
        raise ValueError("TG_BOT_BOT_TRANSPORT 仅支持 long_polling 或 webhook")

    @field_validator("bot_webhook_url", mode="before")
    @classmethod
    def validate_bot_webhook_url(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        url = str(value).strip()
        if any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in url
        ):
            raise ValueError("TG_BOT_BOT_WEBHOOK_URL 不能包含空白或控制字符")
        try:
            parsed = urlsplit(url)
            port = parsed.port
            hostname = parsed.hostname
        except ValueError as exc:
            raise ValueError("TG_BOT_BOT_WEBHOOK_URL 格式无效") from exc
        authority = parsed.netloc.rsplit("@", 1)[-1]
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or "#" in url
            or authority.endswith(":")
            or (port is not None and port not in {80, 88, 443, 8443})
        ):
            raise ValueError(
                "TG_BOT_BOT_WEBHOOK_URL 必须是 Telegram 支持端口上的 HTTPS URL，"
                "且不能包含用户信息或 fragment"
            )
        return url

    @field_validator("bot_webhook_secret")
    @classmethod
    def validate_bot_webhook_secret(cls, value: SecretStr | str | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        secret = secret.strip()
        if not secret:
            return None
        if len(secret) > 256 or re.fullmatch(r"[A-Za-z0-9_-]+", secret) is None:
            raise ValueError("TG_BOT_BOT_WEBHOOK_SECRET 仅支持 1-256 位字母、数字、下划线或连字符")
        return SecretStr(secret)

    @field_validator("notification_timezone")
    @classmethod
    def validate_notification_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("TG_BOT_NOTIFICATION_TIMEZONE 无效") from exc
        return normalized

    @field_validator("admin_origin")
    @classmethod
    def validate_admin_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        origin = value.strip()
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "," in origin
        ):
            raise ValueError("TG_BOT_ADMIN_ORIGIN 必须是唯一的 HTTPS Origin，且不能包含路径")
        return origin.rstrip("/")

    @property
    def admin_chat_id_list(self) -> list[int]:
        return [int(item.strip()) for item in self.admin_chat_ids.split(",") if item.strip()]

    @property
    def notification_chat_id(self) -> int | None:
        chat_ids = self.admin_chat_id_list
        return chat_ids[0] if chat_ids else None

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            url = self.database_url_override.strip()
            if url.startswith("postgres://"):
                return "postgresql+psycopg://" + url[len("postgres://") :]
            if url.startswith("postgresql://"):
                return "postgresql+psycopg://" + url[len("postgresql://") :]
            return url
        if self.database == "sqlite":
            return f"sqlite:///{self.data_dir / 'database.sqlite3'}"
        raise RuntimeError(
            "使用 PostgreSQL 时必须在环境变量中配置 TG_BOT_DATABASE_URL，"
            "例如 postgresql+psycopg://user:password@localhost:5432/tg_bot"
        )

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_path(self) -> Path:
        return self.logs_dir / self.log_file

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def require_api_credentials(self) -> tuple[int, str]:
        if not self.api_id or not self.api_hash:
            raise RuntimeError("请在 .env 中配置 TG_BOT_API_ID 和 TG_BOT_API_HASH")
        return self.api_id, self.api_hash

    def require_admin_config(self) -> tuple[str, str]:
        secret = self.admin_key.get_secret_value() if self.admin_key else ""
        if len(secret.encode("utf-8")) < 32:
            raise RuntimeError(
                "TG_BOT_ADMIN_KEY 未配置或强度不足；请使用 openssl rand -base64 48 生成"
            )
        if not self.admin_origin:
            raise RuntimeError("TG_BOT_ADMIN_ORIGIN 未配置，管理 API 拒绝启动")
        return secret, self.admin_origin
