from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from tg_botx.features.bot.commands import BotCommandService
from tg_botx.features.bot.models import (
    _BINDING_CODE_TTL,
    _CODE_ALPHABET,
    _CODE_GROUP_LENGTH,
    _CODE_GROUPS,
    BindingCodeView,
    BotBindingError,
    hash_binding_code,
    normalize_binding_code,
)
from tg_botx.features.bot.points import BotPointsService
from tg_botx.features.checkin.runtime import (
    CheckinService,
)
from tg_botx.infrastructure.persistence.db import (
    BotAuditLog,
    BotBinding,
    BotBindingCode,
    Database,
    Task,
    utc_now,
)

logger = logging.getLogger(__name__)


class BotManagementService:
    """Application service shared by HTTP, CLI and the Telegram adapter."""

    def __init__(self, database: Database, checkin: CheckinService | None = None):
        self.database = database
        self.commands = BotCommandService(database)
        self.points = BotPointsService(database)

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
        self,
        quantity: int,
        ttl_days: int | None,
        *,
        role: str = "user",
        idempotency_key: str | None = None,
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
            raw = "".join(
                secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_GROUP_LENGTH * _CODE_GROUPS)
            )
            formatted = "-".join(
                raw[index : index + _CODE_GROUP_LENGTH]
                for index in range(0, len(raw), _CODE_GROUP_LENGTH)
            )
            plain.append(formatted)
            generated.append((hash_binding_code(raw), formatted[-4:], expires_at, role))
        request_hash = hashlib.sha256(
            json.dumps(
                {"role": role, "quantity": quantity, "ttlDays": ttl_days}, sort_keys=True
            ).encode()
        ).hexdigest()
        replay = bool(idempotency_key and self.database.get_bot_binding_batch(idempotency_key))
        batch, items = self.database.create_bot_binding_codes(
            generated, idempotency_key=idempotency_key, request_hash=request_hash, ttl_days=ttl_days
        )
        if replay:
            # Idempotent replay cannot recover plaintext; return masked values rather than secrets.
            plain = ["****-****-****" for _ in items]
        return batch.id if batch else str(uuid.uuid4()), list(zip(plain, items, strict=True))

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
            BindingCodeView(
                item.id,
                item.code_hint,
                item.created_at,
                item.expires_at,
                item.role or "user",
                item.used_at,
                item.revoked_at,
            )
            for item in items
        ], total

    def bindings_page(self, *, page: int, page_size: int) -> tuple[list[BotBinding], int]:
        return self.database.list_bot_bindings_page(page=page, page_size=page_size)

    def revoke_code(self, code_id: str) -> bool:
        return self.database.revoke_bot_binding_code(code_id)

    def revoke_binding(self, binding_id: str) -> bool:
        return self.database.revoke_bot_binding(binding_id)

    def checkin_config(self) -> dict[str, int]:
        return self.points.checkin_config()

    def update_checkin_config(self, minimum: int, maximum: int) -> dict[str, int]:
        return self.points.update_checkin_config(minimum, maximum)

    def checkin(self, user_id: int, chat_id: int) -> tuple[str, int, int]:
        return self.points.checkin(user_id, chat_id)

    def command_configs(self) -> list[dict[str, Any]]:
        return self.commands.command_configs()

    def update_command_config(
        self,
        command: str,
        description: str,
        enabled: bool,
        allowed_roles: Sequence[str] | None = None,
        menu_visible: bool | None = None,
        new_command: str | None = None,
    ) -> dict[str, Any]:
        return self.commands.update_command_config(
            command, description, enabled, allowed_roles, menu_visible, new_command
        )

    def create_command_config(
        self,
        command: str,
        description: str,
        enabled: bool = False,
        allowed_roles: Sequence[str] | None = None,
        menu_visible: bool = False,
        executor_type: str = "none",
        executor_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.commands.create_command_config(
            command,
            description,
            enabled,
            allowed_roles,
            menu_visible,
            executor_type,
            executor_config,
        )

    def reorder_command_configs(self, commands: list[str]) -> list[dict[str, Any]]:
        return self.commands.reorder_command_configs(commands)

    @staticmethod
    def _normalize_roles(value: object) -> list[str]:
        return BotCommandService._normalize_roles(value)

    @staticmethod
    def _item_executor_config(item: Any) -> dict[str, Any]:
        return BotCommandService._item_executor_config(item)

    @classmethod
    def _command_item(cls, item: Any, *, roles: list[str] | None = None) -> dict[str, Any]:
        return BotCommandService._command_item(item, roles=roles)

    @staticmethod
    def _menu_visible(item: dict[str, Any]) -> bool:
        return BotCommandService._menu_visible(item)

    @classmethod
    def _item_roles(cls, command: str, item: Any) -> list[str]:
        return BotCommandService._item_roles(command, item)

    def delete_command_config(self, command: str) -> bool:
        return self.commands.delete_command_config(command)

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
