from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TG_BOT_", env_file=".env", extra="ignore")

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
    log_level: str = Field(default="INFO")
    log_file: str = Field(default="tg-bot.log")
    log_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    log_backup_count: int = Field(default=5, ge=0)

    @field_validator("database", mode="before")
    @classmethod
    def normalize_database(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized in {"postgres", "postgresql"}:
            return "postgresql"
        if normalized in {"sqlite", "sqlite3"}:
            return "sqlite"
        raise ValueError("TG_BOT_DATABASE 仅支持 sqlite 或 postgresql")

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
