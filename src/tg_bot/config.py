from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TG_BOT_", env_file=".env", extra="ignore")

    api_id: int | None = Field(default=None)
    api_hash: str | None = Field(default=None)
    data_dir: Path = Field(default=Path("./data"))
    admin_chat_ids: str = Field(default="")

    @property
    def admin_chat_id_list(self) -> list[int]:
        return [int(item.strip()) for item in self.admin_chat_ids.split(",") if item.strip()]

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'database.sqlite3'}"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def require_api_credentials(self) -> tuple[int, str]:
        if not self.api_id or not self.api_hash:
            raise RuntimeError("请在 .env 中配置 TG_BOT_API_ID 和 TG_BOT_API_HASH")
        return self.api_id, self.api_hash
