from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, get_args

from tg_botx.infrastructure.persistence.db import (
    utc_now,
)

"""Interactive Telegram management bot and its binding service."""

logger = logging.getLogger(__name__)

CommandRole = Literal["anonymous", "user", "admin"]
ExecutorType = Literal["none", "http", "builtin_function", "python"]


_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_CODE_GROUP_LENGTH = 4

_CODE_GROUPS = 3

_BINDING_CODE_TTL = timedelta(minutes=10)

_CONFIRM_TTL_SECONDS = 60

_PAGE_SIZE = 8

DEFAULT_BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "查看绑定状态"),
    ("help", "查看帮助"),
    ("bind", "绑定管理权限"),
    ("unbind", "解除绑定"),
    ("tasks", "查看任务列表"),
    ("status", "查看系统状态"),
    ("checkin", "每日签到领取积分"),
)

_ALL_COMMAND_ROLES = get_args(CommandRole)

_DEFAULT_COMMAND_ROLES: dict[str, tuple[str, ...]] = {
    "start": _ALL_COMMAND_ROLES,
    "help": _ALL_COMMAND_ROLES,
    "bind": _ALL_COMMAND_ROLES,
    "unbind": ("user", "admin"),
    "tasks": ("user", "admin"),
    "status": ("user", "admin"),
    "checkin": ("user", "admin"),
}

_COMMAND_NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")

_COMMAND_TYPES = {"system", "custom"}

_EXECUTOR_TYPES = set(get_args(ExecutorType))

_MAX_EXECUTOR_CONFIG_BYTES = 32 * 1024

_CHECKIN_MIN_KEY = "checkin.points_min"

_CHECKIN_MAX_KEY = "checkin.points_max"

_DEFAULT_CHECKIN_MIN = 1

_DEFAULT_CHECKIN_MAX = 10

_WEBHOOK_UPDATE_DEDUPE_LIMIT = 2048


class BotBindingError(RuntimeError):
    pass


class BotCommandValidationError(BotBindingError):
    pass


class BotCommandConflictError(BotBindingError):
    pass


class BotCommandForbiddenError(BotBindingError):
    pass


def hash_binding_code(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").replace(" ", "").upper().encode()).hexdigest()


def normalize_binding_code(code: str) -> str:
    return code.replace("-", "").replace(" ", "").strip().upper()


@dataclass(frozen=True, slots=True)
class BindingCodeView:
    id: str
    hint: str
    created_at: datetime
    expires_at: datetime | None
    role: str
    used_at: datetime | None
    revoked_at: datetime | None

    @property
    def status(self) -> str:
        if self.revoked_at is not None:
            return "revoked"
        if self.used_at is not None:
            return "used"
        if self.expires_at is not None and self.expires_at <= utc_now():
            return "expired"
        return "active"


@dataclass(slots=True)
class BotRuntimeStatus:
    enabled: bool
    configured: bool
    running: bool = False
    last_poll_at: datetime | None = None
    last_error: str | None = None

    @property
    def health(self) -> str:
        if not self.enabled or not self.configured:
            return "unavailable"
        if self.running and self.last_error is None:
            return "healthy"
        return "degraded"
