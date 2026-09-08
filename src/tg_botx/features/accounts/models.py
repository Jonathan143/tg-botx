from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from telethon import TelegramClient

from tg_botx.infrastructure.persistence.db import utc_now
from tg_botx.integrations.telegram import TelegramAccountConfig, create_telethon_client

logger = logging.getLogger(__name__)


def _create_telegram_client(session_path: str, api_id: int, api_hash: str) -> TelegramClient:
    return create_telethon_client(
        TelegramAccountConfig(
            api_id=api_id,
            api_hash=api_hash,
            session_path=Path(session_path),
        )
    )


LoginMethod = Literal["qr", "phone"]
LoginStage = Literal[
    "connecting",
    "phone_required",
    "qr_pending",
    "code_pending",
    "password_pending",
    "completed",
    "failed",
]

_ACTIVE_STAGES = {
    "connecting",
    "phone_required",
    "qr_pending",
    "code_pending",
    "password_pending",
}
_ACCOUNT_NAME = re.compile(r"^[^/\\\x00]{1,100}$")


class AdminAccountError(RuntimeError):
    """An API-safe account-management error with a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class LoginFlowView:
    flow_id: str
    account_name: str
    method: LoginMethod
    stage: LoginStage
    qr_url: str | None = field(repr=False)
    qr_expires_at: datetime | None
    account_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AccountView:
    account_id: str
    name: str
    phone_masked: str | None
    session_name: str
    is_active: bool
    created_at: datetime
    task_count: int
    enabled_task_count: int


@dataclass(frozen=True, slots=True)
class ChatView:
    chat_id: str
    chat_type: Literal["bot", "group", "private"]
    title: str
    username: str | None
    has_avatar: bool
    avatar_photo_id: int | None = None


@dataclass(frozen=True, slots=True)
class ChatPullView:
    account_id: str
    added: int
    updated: int
    removed: int
    total: int
    synced_at: datetime


@dataclass(frozen=True, slots=True)
class MessageProbeView:
    message_id: int
    text: str
    buttons: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class AccountTaskImpact:
    task_id: str
    name: str
    enabled: bool
    archived: bool


@dataclass(frozen=True, slots=True)
class LogoutImpact:
    account_id: str
    account_name: str
    tasks: tuple[AccountTaskImpact, ...]

    @property
    def enabled_task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.tasks if task.enabled)

    @property
    def enabled_task_count(self) -> int:
        return len(self.enabled_task_ids)


@dataclass(slots=True)
class _LoginFlow:
    flow_id: str
    account_name: str
    method: LoginMethod
    stage: LoginStage
    client: Any = field(repr=False)
    connected: bool = False
    qr_url: str | None = field(default=None, repr=False)
    qr_expires_at: datetime | None = None
    account_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    qr_login: Any | None = field(default=None, repr=False)
    waiter: asyncio.Task[None] | None = field(default=None, repr=False)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def view(self) -> LoginFlowView:
        return LoginFlowView(
            flow_id=self.flow_id,
            account_name=self.account_name,
            method=self.method,
            stage=self.stage,
            qr_url=self.qr_url,
            qr_expires_at=self.qr_expires_at,
            account_id=self.account_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )
