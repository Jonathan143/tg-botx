"""Interactive Telegram management bot and its binding service."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tg_botx.config import Settings
from tg_botx.features.checkin.runtime import (
    CheckinService,
    ManualRunConflict,
    TaskNotFound,
    TaskStateError,
    WorkflowVersionNotFound,
)
from tg_botx.infrastructure.persistence.db import (
    BotAuditLog,
    BotBinding,
    BotBindingCode,
    Database,
    Task,
    utc_now,
)
from tg_botx.integrations.telegram_bot import TelegramBotApiClient, TelegramBotApiError

logger = logging.getLogger(__name__)
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
_ALL_COMMAND_ROLES = ("anonymous", "user", "admin")
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
_EXECUTOR_TYPES = {"none", "http", "builtin_function", "python", "javascript"}
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


class BotManagementService:
    """Application service shared by HTTP, CLI and the Telegram adapter."""

    def __init__(self, database: Database, checkin: CheckinService):
        self.database = database
        self.checkin_service = checkin

    def _generate_code(self, role: str, expires_at: datetime | None) -> tuple[str, BotBindingCode]:
        raw = "".join(
            secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_GROUP_LENGTH * _CODE_GROUPS)
        )
        formatted = "-".join(
            raw[index : index + _CODE_GROUP_LENGTH]
            for index in range(0, len(raw), _CODE_GROUP_LENGTH)
        )
        item = self.database.create_bot_binding_code(
            hash_binding_code(raw),
            formatted[-4:],
            expires_at,
            role,
        )
        return formatted, item

    def create_binding_code(self) -> tuple[str, BotBindingCode]:
        return self._generate_code("user", utc_now() + _BINDING_CODE_TTL)

    def create_binding_codes(
        self, quantity: int, ttl_days: int | None, *, role: str = "user", idempotency_key: str | None = None
    ) -> tuple[str, list[tuple[str, BotBindingCode]]]:
        if role not in {"user", "admin"}:
            raise BotBindingError("不支持的绑定身份")
        if quantity < 1 or quantity > 100:
            raise BotBindingError("一次最多生成 100 个绑定码")
        if ttl_days not in {1, 7, 30, None}:
            raise BotBindingError("绑定码有效期无效")
        now = utc_now()
        expires_at = None if ttl_days is None else now + timedelta(days=ttl_days)
        generated: list[tuple[str, str, datetime | None, str]] = []
        plain: list[str] = []
        for _ in range(quantity):
            raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_GROUP_LENGTH * _CODE_GROUPS))
            formatted = "-".join(raw[index:index + _CODE_GROUP_LENGTH] for index in range(0, len(raw), _CODE_GROUP_LENGTH))
            plain.append(formatted)
            generated.append((hash_binding_code(raw), formatted[-4:], expires_at, role))
        request_hash = hashlib.sha256(json.dumps({"role": role, "quantity": quantity, "ttlDays": ttl_days}, sort_keys=True).encode()).hexdigest()
        replay = bool(idempotency_key and self.database.get_bot_binding_batch(idempotency_key))
        batch, items = self.database.create_bot_binding_codes(generated, idempotency_key=idempotency_key, request_hash=request_hash, ttl_days=ttl_days)
        if replay:
            # Idempotent replay cannot recover plaintext; return masked values rather than secrets.
            plain = [f"****-****-****" for _ in items]
        return batch.id if batch else str(uuid.uuid4()), list(zip(plain, items))

    def binding_codes(self) -> list[BindingCodeView]:
        return [
            BindingCodeView(
                item.id,
                item.code_hint,
                item.created_at,
                item.expires_at,
                item.role or "user",
                item.used_at,
                item.revoked_at,
            )
            for item in self.database.list_bot_binding_codes()
        ]

    def bindings(self) -> list[BotBinding]:
        return self.database.list_bot_bindings()

    def binding_codes_page(self, *, page: int, page_size: int) -> tuple[list[BindingCodeView], int]:
        items, total = self.database.list_bot_binding_codes_page(page=page, page_size=page_size)
        return [
            BindingCodeView(item.id, item.code_hint, item.created_at, item.expires_at, item.role or "user", item.used_at, item.revoked_at)
            for item in items
        ], total

    def bindings_page(self, *, page: int, page_size: int) -> tuple[list[BotBinding], int]:
        return self.database.list_bot_bindings_page(page=page, page_size=page_size)

    def revoke_code(self, code_id: str) -> bool:
        return self.database.revoke_bot_binding_code(code_id)

    def revoke_binding(self, binding_id: str) -> bool:
        return self.database.revoke_bot_binding(binding_id)

    def checkin_config(self) -> dict[str, int]:
        getter = getattr(self.database, "get_bot_setting", None)

        def read(key: str, fallback: int) -> int:
            if not callable(getter):
                return fallback
            try:
                value = int(getter(key) or fallback)
            except (TypeError, ValueError):
                return fallback
            return value if value >= 1 else fallback

        minimum = read(_CHECKIN_MIN_KEY, _DEFAULT_CHECKIN_MIN)
        maximum = read(_CHECKIN_MAX_KEY, _DEFAULT_CHECKIN_MAX)
        if maximum < minimum:
            maximum = minimum
        return {"minPoints": minimum, "maxPoints": maximum}

    def update_checkin_config(self, minimum: int, maximum: int) -> dict[str, int]:
        if minimum < 1 or maximum < 1 or minimum > maximum:
            raise BotBindingError("积分随机范围无效，需满足 1 ≤ 最小值 ≤ 最大值")
        if maximum > 1_000_000:
            raise BotBindingError("积分随机范围不能超过 1000000")
        self.database.set_bot_setting(_CHECKIN_MIN_KEY, str(minimum))
        self.database.set_bot_setting(_CHECKIN_MAX_KEY, str(maximum))
        return {"minPoints": minimum, "maxPoints": maximum}

    def checkin(self, user_id: int, chat_id: int) -> tuple[str, int, int]:
        config = self.checkin_config()
        return self.database.checkin_bot_user(
            user_id, chat_id, config["minPoints"], config["maxPoints"]
        )

    def command_configs(self) -> list[dict[str, Any]]:
        stored = {item.command: item for item in self.database.list_bot_command_configs()}
        configs: list[dict[str, Any]] = []
        for default_index, (command, default_description) in enumerate(DEFAULT_BOT_COMMANDS):
            # Keep adapters created before the points feature usable: a
            # storage implementation that does not expose point persistence
            # cannot execute /checkin and should not advertise it.
            if command == "checkin" and not hasattr(self.database, "checkin_bot_user"):
                continue
            item = stored.get(command)
            configs.append(
                {
                    "command": command,
                    "type": "system",
                    "description": item.description if item is not None else default_description,
                    "enabled": item.enabled if item is not None else True,
                    "menuVisible": getattr(item, "menu_visible", item.enabled if item is not None else True),
                    "allowedRoles": self._item_roles(command, item),
                    "executorType": "none",
                    "executorConfig": {},
                    "sortOrder": (
                        getattr(item, "sort_order", None)
                        if item is not None and getattr(item, "sort_order", None) is not None
                        else default_index
                    ),
                    "updatedAt": getattr(item, "updated_at", None).isoformat()
                    if getattr(item, "updated_at", None) is not None
                    else None,
                }
            )
        default_names = {command for command, _ in DEFAULT_BOT_COMMANDS}
        configs.extend(
            {
                "command": item.command,
                "type": getattr(item, "command_type", "custom")
                if getattr(item, "command_type", "custom") in _COMMAND_TYPES
                else "custom",
                "description": item.description,
                "enabled": item.enabled,
                "menuVisible": getattr(item, "menu_visible", item.enabled),
                "allowedRoles": self._item_roles(item.command, item),
                "executorType": getattr(item, "executor_type", "none")
                if getattr(item, "executor_type", "none") in _EXECUTOR_TYPES
                else "none",
                "executorConfig": self._item_executor_config(item),
                "sortOrder": getattr(item, "sort_order", None),
                "updatedAt": getattr(item, "updated_at", None).isoformat()
                if getattr(item, "updated_at", None) is not None
                else None,
            }
            for item in stored.values()
            if item.command not in default_names and _COMMAND_NAME_PATTERN.fullmatch(item.command)
        )
        fallback_custom_order = len(DEFAULT_BOT_COMMANDS)
        return sorted(
            configs,
            key=lambda item: (
                item["sortOrder"] if item["sortOrder"] is not None else fallback_custom_order,
                item["command"],
            ),
        )

    def update_command_config(
        self,
        command: str,
        description: str,
        enabled: bool,
        allowed_roles: list[str] | tuple[str, ...] | None = None,
        menu_visible: bool | None = None,
        new_command: str | None = None,
    ) -> dict[str, Any]:
        if not _COMMAND_NAME_PATTERN.fullmatch(command):
            raise BotCommandValidationError("不支持该管理 Bot 指令")
        description = description.strip()
        if not description or len(description) > 256:
            raise BotCommandValidationError("指令说明不能为空且不能超过 256 个字符")
        current = next(
            (item for item in self.database.list_bot_command_configs() if item.command == command),
            None,
        )
        target_command = (new_command or command).casefold().removeprefix("/")
        if not _COMMAND_NAME_PATTERN.fullmatch(target_command):
            raise BotCommandValidationError("不支持该管理 Bot 指令")
        if target_command != command:
            if command in {name for name, _ in DEFAULT_BOT_COMMANDS}:
                raise BotCommandForbiddenError("系统指令不可修改指令名")
            if target_command in {name for name, _ in DEFAULT_BOT_COMMANDS}:
                raise BotCommandConflictError("该管理 Bot 指令已存在")
            if any(item.command == target_command for item in self.database.list_bot_command_configs()):
                raise BotCommandConflictError("该管理 Bot 指令已存在")
            renamed = self.database.rename_bot_command_config(command, target_command)
            if renamed is None:
                raise ValueError("指令不存在")
            command = target_command
        roles = self._normalize_roles(
            allowed_roles if allowed_roles is not None else self._item_roles(command, current)
        )
        item = self.database.upsert_bot_command_config(
            command,
            description,
            enabled,
            json.dumps(roles, ensure_ascii=False),
            menu_visible=menu_visible,
            command_type="system"
            if command in {name for name, _ in DEFAULT_BOT_COMMANDS}
            else None,
        )
        return self._command_item(item, roles=roles)

    def create_command_config(
        self,
        command: str,
        description: str,
        enabled: bool = False,
        allowed_roles: list[str] | tuple[str, ...] | None = None,
        menu_visible: bool = False,
        executor_type: str = "none",
        executor_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = command.casefold().removeprefix("/")
        if not _COMMAND_NAME_PATTERN.fullmatch(command):
            raise BotCommandValidationError("不支持该管理 Bot 指令")
        description = description.strip()
        if not description or len(description) > 256:
            raise BotCommandValidationError("指令说明不能为空且不能超过 256 个字符")
        if command in {name for name, _ in DEFAULT_BOT_COMMANDS}:
            raise BotCommandConflictError("系统指令不可重复创建")
        if executor_type not in _EXECUTOR_TYPES:
            raise BotCommandValidationError("不支持的指令执行器")
        config = executor_config if executor_config is not None else {}
        if not isinstance(config, dict):
            raise BotCommandValidationError("执行器配置必须是 JSON 对象")
        try:
            encoded_config = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise BotCommandValidationError("执行器配置必须是合法 JSON") from exc
        if len(encoded_config.encode("utf-8")) > _MAX_EXECUTOR_CONFIG_BYTES:
            raise BotCommandValidationError("执行器配置不能超过 32KB")
        roles = self._normalize_roles(allowed_roles or [])
        existing = {item.command.casefold() for item in self.database.list_bot_command_configs()}
        if command in existing:
            raise BotCommandConflictError("该管理 Bot 指令已存在")
        item = self.database.upsert_bot_command_config(
            command,
            description,
            enabled,
            json.dumps(roles, ensure_ascii=False),
            menu_visible=menu_visible,
            command_type="custom",
            executor_type=executor_type,
            executor_config_json=encoded_config,
        )
        return self._command_item(item, roles=roles)

    def reorder_command_configs(self, commands: list[str]) -> list[dict[str, Any]]:
        current = self.command_configs()
        current_names = {item["command"] for item in current}
        normalized = [name.casefold().removeprefix("/") for name in commands]
        if len(normalized) != len(set(normalized)) or set(normalized) != current_names:
            raise BotCommandValidationError("指令排序列表与当前指令不一致")
        by_name = {item["command"]: item for item in current}
        stored_names = {stored.command for stored in self.database.list_bot_command_configs()}
        for index, command in enumerate(normalized):
            item = by_name[command]
            if command not in stored_names:
                self.update_command_config(
                    command,
                    item["description"],
                    item["enabled"],
                    item["allowedRoles"],
                    item.get("menuVisible", True),
                )
            if not self.database.set_bot_command_order(command, index):
                raise ValueError("指令排序保存失败")
        return self.command_configs()

    @staticmethod
    def _normalize_roles(value: object) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            raise BotCommandValidationError("可调用身份配置无效")
        roles = list(dict.fromkeys(value))
        if any(role not in _ALL_COMMAND_ROLES for role in roles):
            raise BotCommandValidationError("可调用身份只能选择未绑定用户、普通用户或管理员")
        if not roles:
            return list(_ALL_COMMAND_ROLES)
        return [str(role) for role in _ALL_COMMAND_ROLES if role in roles]

    @staticmethod
    def _item_executor_config(item: Any) -> dict[str, Any]:
        raw = getattr(item, "executor_config_json", None)
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    return value
            except (TypeError, json.JSONDecodeError):
                pass
        return {}

    @classmethod
    def _command_item(cls, item: Any, *, roles: list[str] | None = None) -> dict[str, Any]:
        return {
            "command": item.command,
            "type": getattr(item, "command_type", "custom")
            if getattr(item, "command_type", "custom") in _COMMAND_TYPES
            else "custom",
            "description": item.description,
            "enabled": item.enabled,
            "menuVisible": getattr(item, "menu_visible", item.enabled),
            "allowedRoles": roles if roles is not None else cls._item_roles(item.command, item),
            "executorType": getattr(item, "executor_type", "none")
            if getattr(item, "executor_type", "none") in _EXECUTOR_TYPES
            else "none",
            "executorConfig": cls._item_executor_config(item),
            "sortOrder": getattr(item, "sort_order", None),
            "updatedAt": getattr(item, "updated_at", None).isoformat()
            if getattr(item, "updated_at", None) is not None
            else None,
        }

    @staticmethod
    def _menu_visible(item: dict[str, Any]) -> bool:
        return bool(item.get("menuVisible", item.get("enabled", False)))

    @classmethod
    def _item_roles(cls, command: str, item: Any) -> list[str]:
        raw = getattr(item, "allowed_roles_json", None) if item is not None else None
        if isinstance(raw, str):
            try:
                return cls._normalize_roles(json.loads(raw))
            except (ValueError, TypeError, json.JSONDecodeError, BotBindingError):
                pass
        if item is not None and raw is None:
            return list(_ALL_COMMAND_ROLES)
        return list(_DEFAULT_COMMAND_ROLES.get(command, ("user", "admin")))

    def delete_command_config(self, command: str) -> bool:
        """Delete a command from the persisted menu.

        Built-in commands are protected and cannot be deleted.
        """
        if not _COMMAND_NAME_PATTERN.fullmatch(command):
            raise BotBindingError("不支持该管理 Bot 指令")
        default = next(
            (description for name, description in DEFAULT_BOT_COMMANDS if name == command), None
        )
        if default is None:
            return self.database.delete_bot_command_config(command)
        raise BotCommandForbiddenError("系统指令不可删除")

    def bind(self, code: str, *, user_id: int, chat_id: int, user: dict[str, Any]) -> BotBinding:
        normalized = normalize_binding_code(code)
        if len(normalized) != _CODE_GROUP_LENGTH * _CODE_GROUPS:
            raise BotBindingError("绑定码格式无效")
        if self.database.get_bot_binding(user_id) is not None:
            raise BotBindingError("该用户已绑定，请先解除现有绑定")
        binding = self.database.consume_bot_binding_code(
            hash_binding_code(normalized),
            user_id=user_id,
            chat_id=chat_id,
            username=self._string_or_none(user.get("username")),
            first_name=self._string_or_none(user.get("first_name")),
            last_name=self._string_or_none(user.get("last_name")),
        )
        if binding is None:
            raise BotBindingError("绑定码无效、已使用、已撤销或已过期")
        self.audit(user_id, chat_id, "bind", "success")
        return binding

    def unbind(self, user_id: int, chat_id: int) -> bool:
        binding = self.database.get_bot_binding(user_id)
        if binding is None or binding.chat_id != chat_id:
            return False
        result = self.database.revoke_bot_binding(binding.id)
        self.audit(user_id, chat_id, "unbind", "success" if result else "failed")
        return result

    def is_bound(self, user_id: int, chat_id: int) -> bool:
        binding = self.database.get_bot_binding(user_id)
        return binding is not None and binding.chat_id == chat_id

    def binding_role(self, user_id: int, chat_id: int) -> str | None:
        binding = self.database.get_bot_binding(user_id)
        if binding is None or binding.chat_id != chat_id:
            return None
        return binding.role or "user"

    def is_admin(self, user_id: int, chat_id: int) -> bool:
        return self.binding_role(user_id, chat_id) == "admin"

    def audit(
        self,
        user_id: int | None,
        chat_id: int | None,
        action: str,
        result: str,
        *,
        task: Task | None = None,
        update_id: int | None = None,
        details: str | None = None,
    ) -> None:
        self.database.add_bot_audit_log(
            BotAuditLog(
                actor_user_id=user_id,
                actor_chat_id=chat_id,
                action=action,
                task_id=task.id if task else None,
                task_name=task.name if task else None,
                result=result,
                update_id=update_id,
                details=details[:500] if details else None,
            )
        )

    @staticmethod
    def _string_or_none(value: object) -> str | None:
        return value if isinstance(value, str) else None


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


class TelegramManagementBot:
    def __init__(self, settings: Settings, database: Database, checkin: CheckinService):
        token = (
            settings.admin_bot_token.get_secret_value().strip() if settings.admin_bot_token else ""
        )
        self.database = database
        self.checkin = checkin
        self.management = BotManagementService(database, checkin)
        self.client = TelegramBotApiClient(token) if token else None
        # Keep the adapter tolerant of older injected settings objects.  The
        # concrete ``Settings`` model always exposes these fields, while
        # lightweight callers may still only provide the long-polling config.
        self.transport = getattr(settings, "bot_transport", "long_polling")
        self.webhook_url = getattr(settings, "bot_webhook_url", None)
        webhook_secret = getattr(settings, "bot_webhook_secret", None)
        if webhook_secret:
            get_secret_value = getattr(webhook_secret, "get_secret_value", None)
            raw_secret = get_secret_value() if callable(get_secret_value) else webhook_secret
            self._webhook_secret = raw_secret.strip() if isinstance(raw_secret, str) else ""
        else:
            self._webhook_secret = ""
        webhook_configured = bool(self.webhook_url and self._webhook_secret)
        configured = bool(token) and (self.transport != "webhook" or webhook_configured)
        self.status = BotRuntimeStatus(settings.bot_enabled, configured)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._offset: int | None = None
        self._webhook_update_lock = asyncio.Lock()
        self._webhook_seen_update_ids: set[int] = set()
        self._webhook_seen_update_order: deque[int] = deque()

    async def start(self) -> None:
        if not self.status.enabled:
            logger.info("TG_BOT_BOT_ENABLED=false，Telegram 管理 Bot 未启动")
            return
        if self.client is None:
            logger.error("未配置 TG_BOT_ADMIN_BOT_TOKEN，Telegram 管理 Bot 已禁用")
            return
        if self.transport == "webhook" and not self.status.configured:
            logger.error(
                "Webhook 模式需要配置 TG_BOT_BOT_WEBHOOK_URL 和 "
                "TG_BOT_BOT_WEBHOOK_SECRET，Telegram 管理 Bot 已禁用"
            )
            return
        if self._task is not None and not self._task.done():
            return
        # ``setWebhook``/``deleteWebhook`` both discard updates accumulated
        # before this session.  A new session therefore must not inherit the
        # previous in-memory duplicate window.
        self._webhook_seen_update_ids.clear()
        self._webhook_seen_update_order.clear()
        self.status.running = False
        self._stop.clear()
        # Polling offsets are intentionally session-scoped. Both transports
        # ask Telegram to discard updates accumulated before startup.
        self._offset = None
        if self.transport == "webhook":
            self._task = asyncio.create_task(
                self._webhook_registration_loop(), name="telegram-management-bot-webhook"
            )
        else:
            self._task = asyncio.create_task(
                self._poll_loop(), name="telegram-management-bot-polling"
            )

    def command_configs(self) -> list[dict[str, Any]]:
        return self.management.command_configs()

    async def pull_remote_commands(self) -> list[dict[str, Any]]:
        """Pull Telegram's default command menu into the local database."""
        if self.client is None:
            return self.command_configs()
        remote = await self.client.get_commands(scope={"type": "default"})
        remote_by_name: dict[str, str] = {}
        for item in remote:
            command = item["command"].casefold().removeprefix("/")
            description = item["description"].strip()
            if _COMMAND_NAME_PATTERN.fullmatch(command) and description:
                remote_by_name[command] = description
        default_names = {name for name, _ in DEFAULT_BOT_COMMANDS}
        current_configs = {item["command"]: item for item in self.command_configs()}
        for command, default_description in DEFAULT_BOT_COMMANDS:
            description = remote_by_name.get(command)
            if description is None:
                current = current_configs.get(command)
                description = current["description"] if current is not None else default_description
                menu_visible = False
            else:
                menu_visible = True
            self.management.update_command_config(
                command,
                description,
                current_configs.get(command, {}).get("enabled", True),
                menu_visible=menu_visible,
            )
        for command, description in remote_by_name.items():
            # Telegram synchronization only owns the built-in command set;
            # custom commands are managed exclusively from the admin UI.
            if command in default_names:
                current = current_configs.get(command, {})
                self.management.update_command_config(
                    command,
                    description,
                    current.get("enabled", True),
                    menu_visible=True,
                )
        return self.command_configs()

    async def sync_remote_commands(self) -> list[dict[str, Any]]:
        """Backward-compatible alias for the explicit pull operation."""
        return await self.pull_remote_commands()

    async def refresh_commands(self) -> None:
        """Push the configured command menu to Telegram immediately."""
        if self.client is None:
            return
        commands = [
            {"command": item["command"], "description": item["description"]}
            for item in self.command_configs()
            if BotManagementService._menu_visible(item)
        ]
        # Telegram resolves a private-chat scope before the default scope. A
        # stale private-chat menu therefore masks a newly configured default
        # menu. Keep both managed scopes identical so omitted commands are
        # replaced in either location.
        managed_scopes: tuple[dict[str, object], ...] = (
            {"type": "default"},
            {"type": "all_private_chats"},
        )
        for scope in managed_scopes:
            if commands:
                await self.client.set_commands(commands, scope=scope)
            else:
                await self.client.delete_commands(scope=scope)

        # This bot ignores non-private messages. Remove historical menus from
        # group scopes instead of advertising commands that cannot run there.
        unused_scopes: tuple[dict[str, object], ...] = (
            {"type": "all_group_chats"},
            {"type": "all_chat_administrators"},
        )
        for scope in unused_scopes:
            await self.client.delete_commands(scope=scope)

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.client is not None:
            await self.client.close()

    def public_status(self) -> dict[str, Any]:
        return {
            "enabled": self.status.enabled,
            "configured": self.status.configured,
            "running": self.status.running,
            "health": self.status.health,
            "transport": self.transport,
            "lastPollAt": self.status.last_poll_at.isoformat()
            if self.status.last_poll_at
            else None,
            "lastError": self.status.last_error,
        }

    def webhook_secret_matches(self, secret: str | None) -> bool:
        """Return whether a request carries this bot's configured secret."""

        configured_secret = getattr(self, "_webhook_secret", "")
        if (
            getattr(self, "transport", None) != "webhook"
            or not isinstance(configured_secret, str)
            or not configured_secret
        ):
            return False
        if not isinstance(secret, str) or not secret or len(secret) > 256:
            return False
        try:
            candidate = secret.encode("ascii")
        except UnicodeEncodeError:
            # ``hmac.compare_digest(str, str)`` raises for non-ASCII input;
            # an invalid header should be a normal authentication failure.
            return False
        try:
            expected = configured_secret.encode("ascii")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(candidate, expected)

    def accepts_webhook(self, secret: str | None) -> bool:
        """Authenticate and authorize an update for normal processing."""

        stop_event = getattr(self, "_stop", None)
        if stop_event is not None and stop_event.is_set():
            return False
        return (
            self.status.enabled
            and self.status.configured
            # Do not accept Telegram retries until setWebhook has completed
            # with drop_pending_updates=True. This keeps commands sent while
            # the service was offline from slipping through during startup.
            and self.status.running
            and self.webhook_secret_matches(secret)
        )

    def _claim_webhook_update(self, update: dict[str, object]) -> bool:
        """Claim an update id once, retaining only a bounded recent window."""

        update_id = update.get("update_id")
        if type(update_id) is not int:
            return True
        seen = getattr(self, "_webhook_seen_update_ids", None)
        if seen is None:
            seen = set()
            self._webhook_seen_update_ids = seen
        if update_id in seen:
            return False
        seen.add(update_id)
        order = getattr(self, "_webhook_seen_update_order", None)
        if order is None:
            order = deque()
            self._webhook_seen_update_order = order
        order.append(update_id)
        while len(order) > _WEBHOOK_UPDATE_DEDUPE_LIMIT:
            seen.discard(order.popleft())
        return True

    def _webhook_update_guard(self) -> asyncio.Lock:
        lock = getattr(self, "_webhook_update_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._webhook_update_lock = lock
        return lock

    async def discard_webhook_update(self, update: dict[str, object]) -> None:
        """Acknowledge an authenticated update without executing it."""

        async with self._webhook_update_guard():
            self._claim_webhook_update(update)

    async def handle_webhook_update(self, update: dict[str, object]) -> None:
        """Process one authenticated Telegram webhook update."""

        async with self._webhook_update_guard():
            # The route performs the normal readiness check before parsing the
            # body, but shutdown can begin while a request is waiting for this
            # lock.  Re-check the stop gate here so queued requests are
            # acknowledged and discarded instead of starting during teardown.
            stop_event = getattr(self, "_stop", None)
            if stop_event is not None and stop_event.is_set():
                self._claim_webhook_update(update)
                return
            if not self._claim_webhook_update(update):
                return
            self.status.last_poll_at = utc_now()
            try:
                await self._handle_update(update)
                self.status.last_error = None
            except Exception as exc:
                self.status.last_error = type(exc).__name__
                logger.exception("管理 Bot 处理 Webhook update 失败 type=%s", type(exc).__name__)

    async def _webhook_registration_loop(self) -> None:
        assert self.client is not None
        assert self.webhook_url is not None
        try:
            while not self._stop.is_set():
                try:
                    await self.client.set_webhook(self.webhook_url, self._webhook_secret)
                    if self._stop.is_set():
                        break
                    self.status.running = True
                    self.status.last_error = None
                    logger.info("Telegram 管理 Bot 已使用 Webhook 模式启动")
                    await self._stop.wait()
                except asyncio.CancelledError:
                    raise
                except TelegramBotApiError as exc:
                    self.status.running = False
                    self.status.last_error = type(exc).__name__
                    logger.warning("管理 Bot 注册 Webhook 失败")
                    await asyncio.sleep(min(exc.retry_after or 5, 30))
                except Exception as exc:
                    self.status.running = False
                    self.status.last_error = type(exc).__name__
                    logger.warning("管理 Bot 注册 Webhook 失败 type=%s", type(exc).__name__)
                    await asyncio.sleep(5)
        finally:
            self.status.running = False

    async def _poll_loop(self) -> None:
        assert self.client is not None
        self.status.running = True
        try:
            # getUpdates and Webhook are mutually exclusive. Remove any
            # previously registered Webhook and discard stale interactive
            # commands before entering the polling loop.
            while not self._stop.is_set():
                try:
                    await self.client.delete_webhook(drop_pending_updates=True)
                    break
                except asyncio.CancelledError:
                    raise
                except TelegramBotApiError as exc:
                    self.status.last_error = type(exc).__name__
                    await asyncio.sleep(min(exc.retry_after or 5, 30))
                except Exception as exc:
                    self.status.last_error = type(exc).__name__
                    logger.warning("管理 Bot 启动清理失败 type=%s", type(exc).__name__)
                    await asyncio.sleep(5)
            while not self._stop.is_set():
                try:
                    updates = await self.client.get_updates(self._offset)
                    self.status.last_poll_at = utc_now()
                    self.status.last_error = None
                    for update in updates:
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            self._offset = update_id + 1
                        try:
                            await self._handle_update(update)
                        except Exception as exc:
                            self.status.last_error = type(exc).__name__
                            logger.exception(
                                "管理 Bot 处理 update 失败 type=%s", type(exc).__name__
                            )
                except asyncio.CancelledError:
                    raise
                except TelegramBotApiError as exc:
                    self.status.last_error = type(exc).__name__
                    await asyncio.sleep(min(exc.retry_after or 5, 30))
                except Exception as exc:
                    self.status.last_error = type(exc).__name__
                    logger.warning("管理 Bot 轮询失败 type=%s", type(exc).__name__)
                    await asyncio.sleep(5)
        finally:
            self.status.running = False

    async def _handle_update(self, update: dict[str, object]) -> None:
        message = update.get("message")
        if isinstance(message, dict):
            await self._handle_message(message, update.get("update_id"))
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_callback(callback, update.get("update_id"))

    async def _handle_message(self, message: dict[str, object], update_id: object) -> None:
        chat = message.get("chat")
        user = message.get("from")
        text = message.get("text")
        if (
            not isinstance(chat, dict)
            or chat.get("type") != "private"
            or not isinstance(user, dict)
        ):
            return
        user_id, chat_id = self._ids(user, chat)
        if user_id is None or chat_id is None or not isinstance(text, str):
            return
        parsed = text.strip().split(maxsplit=1)
        if not parsed or not parsed[0].startswith("/"):
            return
        command = parsed[0][1:].split("@", 1)[0].casefold()
        canonical_command = "tasks" if command == "task" else command
        argument = parsed[1].strip() if len(parsed) > 1 else ""
        update_number = update_id if isinstance(update_id, int) else None
        config = next(
            (item for item in self.command_configs() if item["command"] == canonical_command),
            None,
        )
        if config is None:
            await self._send(chat_id, "无法识别该命令，请发送 /help 查看可用命令。")
            return
        if not config["enabled"]:
            await self._send(chat_id, "该命令当前已停用，请联系管理员。")
            return
        if not self._command_allowed(user_id, chat_id, canonical_command, update_number):
            await self._send(chat_id, "你没有权限调用该命令。")
            return
        if config.get("type") == "custom":
            await self._send(chat_id, "该自定义指令的执行器尚未实现，请联系管理员。")
            return
        if command == "start":
            await self._send(chat_id, self._welcome(user_id, chat_id))
        elif command == "help":
            await self._send(chat_id, self._help(user_id, chat_id))
        elif command == "bind":
            await self._bind(chat_id, user_id, user, argument, update_number)
        elif command == "unbind":
            if self.management.unbind(user_id, chat_id):
                await self._send(chat_id, "✅ 已解除管理 Bot 绑定。")
            else:
                await self._send(chat_id, "当前没有可解除的绑定。")
        elif canonical_command == "tasks":
            await self._send_tasks(chat_id, 1)
        elif command == "status":
            await self._send(chat_id, self._system_status())
        elif command == "checkin":
            status, amount, total = self.management.checkin(user_id, chat_id)
            if status == "success":
                self.management.audit(user_id, chat_id, "checkin", "success", update_id=update_number)
                await self._send(chat_id, f"✅ 签到成功，获得 {amount} 积分！\n当前积分：{total}")
            elif status == "already":
                await self._send(chat_id, f"你今天已经签到过了。\n当前积分：{total}")
            else:
                self.management.audit(user_id, chat_id, "checkin", "denied", update_id=update_number)
                await self._send(chat_id, "请先绑定用户后再签到。")
        else:
            await self._send(chat_id, "无法识别该命令，请发送 /help 查看可用命令。")

    async def _bind(
        self, chat_id: int, user_id: int, user: dict[str, object], code: str, update_id: int | None
    ) -> None:
        if not code:
            await self._send(chat_id, "用法：<code>/bind ABCD-EFGH-IJKL</code>")
            return
        try:
            self.management.bind(code, user_id=user_id, chat_id=chat_id, user=user)
        except BotBindingError as exc:
            self.management.audit(
                user_id, chat_id, "bind", "failed", update_id=update_id, details=str(exc)
            )
            await self._send(chat_id, f"❌ {html.escape(str(exc))}")
            return
        await self._send(chat_id, "✅ 绑定成功。现在可以使用 /tasks、/status 和 /checkin。")

    async def _handle_callback(self, callback: dict[str, object], update_id: object) -> None:
        callback_id = callback.get("id")
        user = callback.get("from")
        data = callback.get("data")
        message = callback.get("message")
        if (
            not isinstance(callback_id, str)
            or not isinstance(user, dict)
            or not isinstance(data, str)
        ):
            return
        try:
            if not isinstance(message, dict):
                return
            chat = message.get("chat")
            message_id = message.get("message_id")
            if (
                not isinstance(chat, dict)
                or chat.get("type") != "private"
                or not isinstance(message_id, int)
            ):
                return
            user_id, chat_id = self._ids(user, chat)
            if user_id is None or chat_id is None:
                return
            update_number = update_id if isinstance(update_id, int) else None
            parts = data.split(":")
            command = "tasks" if parts and parts[0] in {"tasks", "task", "back", "ask", "do"} else None
            if command is None or not self._command_allowed(user_id, chat_id, command, update_number):
                if self.client:
                    await self.client.answer_callback(callback_id, "你没有权限调用该命令")
                return
            if self.client:
                await self.client.answer_callback(callback_id)
            if len(parts) == 2 and parts[0] == "tasks":
                await self._edit_tasks(chat_id, message_id, self._page(parts[1]))
            elif len(parts) == 2 and parts[0] == "task":
                await self._edit_task(chat_id, message_id, parts[1])
            elif len(parts) == 3 and parts[0] == "back" and parts[1] == "tasks":
                await self._edit_tasks(chat_id, message_id, self._page(parts[2]))
            elif len(parts) == 4 and parts[0] == "ask":
                await self._edit_confirmation(chat_id, message_id, parts[1], parts[2], parts[3])
            elif len(parts) == 4 and parts[0] == "do":
                await self._perform_action(
                    user_id, chat_id, message_id, parts[1], parts[2], parts[3], update_number
                )
        except TelegramBotApiError as exc:
            logger.warning("管理 Bot 回调响应失败 type=%s", type(exc).__name__)

    def _authorized(self, user_id: int, chat_id: int, update_id: int | None, action: str) -> bool:
        allowed = self.management.is_bound(user_id, chat_id)
        if not allowed:
            self.management.audit(user_id, chat_id, action, "denied", update_id=update_id)
        return allowed

    def _command_allowed(
        self, user_id: int, chat_id: int, command: str, update_id: int | None
    ) -> bool:
        role = self.management.binding_role(user_id, chat_id) or "anonymous"
        config = next(
            (item for item in self.command_configs() if item["command"] == command), None
        )
        allowed = bool(config and config.get("enabled") and role in config.get("allowedRoles", []))
        if not allowed:
            self.management.audit(user_id, chat_id, command, "denied", update_id=update_id)
        return allowed

    async def _send_tasks(self, chat_id: int, page: int) -> None:
        await self._send(chat_id, *self._task_page(page))

    async def _edit_tasks(self, chat_id: int, message_id: int, page: int) -> None:
        text, markup = self._task_page(page)
        if self.client:
            await self.client.edit_message(chat_id, message_id, text, markup)

    def _task_page(self, page: int) -> tuple[str, dict[str, object]]:
        _, total = self.database.list_tasks_page(page=1, page_size=_PAGE_SIZE)
        pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = min(max(page, 1), pages)
        items, _ = self.database.list_tasks_page(page=page, page_size=_PAGE_SIZE)
        lines = [f"<b>任务列表</b>（第 {page}/{pages} 页，共 {total} 个）"]
        buttons: list[list[dict[str, str]]] = []
        for task in items:
            state = "🟢" if task.enabled else "⚪"
            if task.archived:
                state = "📦"
            lines.append(f"{state} {html.escape(task.name)}")
            buttons.append(
                [{"text": f"{state} {task.name[:45]}", "callback_data": f"task:{task.id}"}]
            )
        if not items:
            lines.append("暂无任务。")
        navigation: list[dict[str, str]] = []
        if page > 1:
            navigation.append({"text": "‹ 上一页", "callback_data": f"tasks:{page - 1}"})
        if page < pages:
            navigation.append({"text": "下一页 ›", "callback_data": f"tasks:{page + 1}"})
        if navigation:
            buttons.append(navigation)
        return "\n".join(lines), {"inline_keyboard": buttons}

    async def _edit_task(self, chat_id: int, message_id: int, task_id: str) -> None:
        if self.client:
            await self.client.edit_message(chat_id, message_id, *self._task_detail(task_id))

    def _task_detail(self, task_id: str) -> tuple[str, dict[str, object]]:
        task = self.database.get_task_any(task_id)
        if task is None:
            return "❌ 任务不存在或已被删除。", {"inline_keyboard": []}
        account = self.database.get_account_by_id(task.account_id)
        last_run = self.database.task_history(task.id, limit=1)
        version = self.database.get_latest_workflow_version(task.id)
        status = "归档" if task.archived else "启用" if task.enabled else "停用"
        run_status = last_run[0].status if last_run else task.last_status or "暂无"
        lines = [
            f"<b>{html.escape(task.name)}</b>",
            f"账号：{html.escape(account.name if account else '未知')}",
            f"目标：{html.escape(task.target)}",
            f"状态：{status}",
            f"下次执行：{self._format_time(task.next_run_at, task.timezone)}",
            f"上次结果：{html.escape(run_status)}",
            f"当前运行：{'是' if self.checkin.running.get(task.id) or self.database.has_running_run(task.id) else '否'}",
            f"发布版本：{version.version_number if version else '未发布'}",
        ]
        buttons: list[list[dict[str, str]]] = []
        if not task.archived:
            action = "disable" if task.enabled else "enable"
            label = "停用任务" if task.enabled else "启用任务"
            buttons.append([{"text": label, "callback_data": f"ask:{action}:{task.id}:0"}])
            buttons.append([{"text": "执行任务", "callback_data": f"ask:run:{task.id}:0"}])
        buttons.append([{"text": "‹ 返回任务列表", "callback_data": "back:tasks:1"}])
        return "\n".join(lines), {"inline_keyboard": buttons}

    async def _edit_confirmation(
        self, chat_id: int, message_id: int, action: str, task_id: str, _: str
    ) -> None:
        task = self.database.get_task_any(task_id)
        if task is None:
            text = "❌ 任务不存在。"
            markup: dict[str, object] = {"inline_keyboard": []}
        else:
            labels = {"enable": "启用", "disable": "停用", "run": "执行"}
            expires = int(time.time()) + _CONFIRM_TTL_SECONDS
            text = f"确认{labels.get(action, '操作')}任务 <b>{html.escape(task.name)}</b>？\n确认按钮将在 60 秒后失效。"
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "确认", "callback_data": f"do:{action}:{task_id}:{expires}"},
                        {"text": "取消", "callback_data": f"task:{task_id}"},
                    ]
                ]
            }
        if self.client:
            await self.client.edit_message(chat_id, message_id, text, markup)

    async def _perform_action(
        self,
        user_id: int,
        chat_id: int,
        message_id: int,
        action: str,
        task_id: str,
        expires: str,
        update_id: int | None,
    ) -> None:
        try:
            if not self.management.is_admin(user_id, chat_id):
                raise BotBindingError("需要管理员权限才能执行此操作")
            if int(expires) < int(time.time()):
                raise BotBindingError("确认按钮已过期，请重新点击操作")
            task = self.database.get_task_any(task_id)
            if task is None:
                raise TaskNotFound("任务不存在")
            if action == "enable":
                task = self.checkin.enable_task(task_id)
                result = "success"
            elif action == "disable":
                task = self.checkin.disable_task(task_id)
                result = "success"
            elif action == "run":
                self.checkin.start_manual_run(task_id)
                result = "success"
            else:
                raise BotBindingError("未知操作")
            self.management.audit(user_id, chat_id, action, result, task=task, update_id=update_id)
            prefix = {
                "enable": "✅ 任务已启用",
                "disable": "✅ 任务已停用",
                "run": "✅ 任务已开始执行",
            }[action]
            text, markup = self._task_detail(task_id)
            text = f"{prefix}\n\n{text}"
        except (
            ValueError,
            BotBindingError,
            TaskNotFound,
            TaskStateError,
            ManualRunConflict,
            WorkflowVersionNotFound,
        ) as exc:
            task = self.database.get_task_any(task_id)
            self.management.audit(
                user_id, chat_id, action, "failed", task=task, update_id=update_id, details=str(exc)
            )
            text, markup = self._task_detail(task_id)
            text = f"❌ {html.escape(str(exc))}\n\n{text}"
        if self.client:
            await self.client.edit_message(chat_id, message_id, text, markup)

    async def _send(self, chat_id: int, text: str, markup: dict[str, object] | None = None) -> None:
        if self.client:
            chunks = [text[index : index + 4000] for index in range(0, len(text), 4000)] or [""]
            for index, chunk in enumerate(chunks):
                await self.client.send_message(
                    chat_id, chunk, markup if index == len(chunks) - 1 else None
                )

    def _welcome(self, user_id: int, chat_id: int) -> str:
        if self.management.is_bound(user_id, chat_id):
            role = self.management.binding_role(user_id, chat_id) or "anonymous"
            available = {
                item["command"]
                for item in self.command_configs()
                if BotManagementService._menu_visible(item)
                and role in item.get("allowedRoles", [])
            }
            actions = []
            if "tasks" in available:
                actions.append("发送 /tasks 查看任务")
            if "status" in available:
                actions.append("发送 /status 查看系统状态")
            if "checkin" in available:
                actions.append("发送 /checkin 领取每日积分")
            suffix = "\n\n" + "，".join(actions) + "。" if actions else ""
            role_label = "管理员" if self.management.is_admin(user_id, chat_id) else "普通用户"
            return f"👋 你已绑定{role_label}身份。{suffix}"
        return "👋 欢迎使用 tg-bot 管理 Bot。\n\n请使用后台生成的绑定码发送：\n<code>/bind ABCD-EFGH-IJKL</code>"

    def _help(self, user_id: int | None = None, chat_id: int | None = None) -> str:
        lines = ["<b>tg-bot 管理 Bot</b>"]
        role = (
            self.management.binding_role(user_id, chat_id) or "anonymous"
            if user_id is not None and chat_id is not None
            else None
        )
        for item in self.command_configs():
            if BotManagementService._menu_visible(item) and (
                role is None or role in item.get("allowedRoles", [])
            ):
                lines.append(f"/{item['command']} {html.escape(item['description'])}")
        return "\n\n".join(lines)

    def _system_status(self) -> str:
        accounts = self.database.list_accounts()
        since = utc_now() - timedelta(hours=24)
        stats = self.database.dashboard_stats(since)
        scheduler = self.checkin.scheduler.running
        lines = [
            "<b>系统状态</b>",
            f"服务：{'正常' if scheduler else '异常'}",
            "数据库：正常",
            f"调度器：{'运行中' if scheduler else '已停止'}",
            f"Telegram 账号：{sum(account.is_active for account in accounts)}/{len(accounts)} 活跃",
            f"任务：{stats['tasks_enabled']}/{stats['tasks_total']} 启用，{len(self.checkin.running)} 运行中",
            f"近 24 小时：成功 {stats['runs_success']}，失败 {stats['runs_failed']}，取消 {stats['runs_canceled']}",
            f"管理 Bot：{'运行中' if self.status.running else '未运行'}",
        ]
        if self.status.last_error:
            lines.append(f"Bot 最近错误：{html.escape(self.status.last_error)}")
        return "\n".join(lines)

    @staticmethod
    def _ids(user: dict[str, object], chat: dict[str, object]) -> tuple[int | None, int | None]:
        user_id = user.get("id")
        chat_id = chat.get("id")
        return (
            user_id if isinstance(user_id, int) else None,
            chat_id if isinstance(chat_id, int) else None,
        )

    @staticmethod
    def _page(value: str) -> int:
        try:
            return max(1, int(value))
        except ValueError:
            return 1

    @staticmethod
    def _format_time(value: datetime | None, timezone_name: str) -> str:
        if value is None:
            return "未安排"
        zone: tzinfo
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            zone = UTC
        return value.astimezone(zone).strftime("%Y-%m-%d %H:%M") + f" ({timezone_name})"
