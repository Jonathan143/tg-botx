from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from tg_botx.core.time import utc_isoformat
from tg_botx.features.bot.executors import (
    CommandConfigError,
    executor_config_error,
    validate_executor_config,
)
from tg_botx.features.bot.models import (
    _ALL_COMMAND_ROLES,
    _COMMAND_NAME_PATTERN,
    _COMMAND_TYPES,
    _DEFAULT_COMMAND_ROLES,
    _EXECUTOR_TYPES,
    _MAX_EXECUTOR_CONFIG_BYTES,
    DEFAULT_BOT_COMMANDS,
    BotBindingError,
    BotCommandConflictError,
    BotCommandForbiddenError,
    BotCommandValidationError,
)
from tg_botx.infrastructure.persistence.db import (
    Database,
)

logger = logging.getLogger(__name__)


class BotCommandService:
    def __init__(self, database: Database):
        self.database = database

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
                    "menuVisible": getattr(
                        item, "menu_visible", item.enabled if item is not None else True
                    ),
                    "allowedRoles": self._item_roles(command, item),
                    "executorType": "none",
                    "executorConfig": {},
                    "sortOrder": (
                        getattr(item, "sort_order", None)
                        if item is not None and getattr(item, "sort_order", None) is not None
                        else default_index
                    ),
                    "updatedAt": utc_isoformat(getattr(item, "updated_at", None)),
                }
            )
        default_names = {command for command, _ in DEFAULT_BOT_COMMANDS}
        for item in stored.values():
            if item.command in default_names or not _COMMAND_NAME_PATTERN.fullmatch(item.command):
                continue
            executor_type = self._item_executor_type(item)
            executor_config = self._item_executor_config(item)
            executor_error = self._executor_error(item, executor_type, executor_config)
            configs.append(
                {
                    "command": item.command,
                    "type": getattr(item, "command_type", "custom")
                    if getattr(item, "command_type", "custom") in _COMMAND_TYPES
                    else "custom",
                    # Legacy rows with an unsupported executor are exposed as
                    # disabled until an administrator replaces or removes them.
                    "enabled": bool(item.enabled) and executor_error is None,
                    "description": item.description,
                    "menuVisible": getattr(item, "menu_visible", item.enabled),
                    "allowedRoles": self._item_roles(item.command, item),
                    "executorType": executor_type,
                    "executorConfig": executor_config,
                    "executorError": executor_error,
                    "sortOrder": getattr(item, "sort_order", None),
                    "updatedAt": utc_isoformat(getattr(item, "updated_at", None)),
                }
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
        allowed_roles: Sequence[str] | None = None,
        menu_visible: bool | None = None,
        new_command: str | None = None,
        *,
        executor_type: str | None = None,
        executor_config: dict[str, Any] | None = None,
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
        default_names = {name for name, _ in DEFAULT_BOT_COMMANDS}
        if command in default_names and (executor_type is not None or executor_config is not None):
            raise BotCommandForbiddenError("系统指令不可配置自定义执行器")
        target_command = (new_command or command).casefold().removeprefix("/")
        if not _COMMAND_NAME_PATTERN.fullmatch(target_command):
            raise BotCommandValidationError("不支持该管理 Bot 指令")
        if target_command != command:
            if command in {name for name, _ in DEFAULT_BOT_COMMANDS}:
                raise BotCommandForbiddenError("系统指令不可修改指令名")
            if target_command in {name for name, _ in DEFAULT_BOT_COMMANDS}:
                raise BotCommandConflictError("该管理 Bot 指令已存在")
            if any(
                item.command == target_command for item in self.database.list_bot_command_configs()
            ):
                raise BotCommandConflictError("该管理 Bot 指令已存在")
            renamed = self.database.rename_bot_command_config(command, target_command)
            if renamed is None:
                raise ValueError("指令不存在")
            command = target_command
        roles = self._normalize_roles(
            allowed_roles if allowed_roles is not None else self._item_roles(command, current)
        )
        if command not in default_names:
            current_type = self._item_executor_type(current) if current is not None else "none"
            current_config = self._item_executor_config(current) if current is not None else {}
            selected_type = executor_type if executor_type is not None else current_type
            # A type change must not inherit keys belonging to the old type.
            selected_config = (
                executor_config
                if executor_config is not None
                else (current_config if executor_type is None else {})
            )
            try:
                validate_executor_config(
                    selected_type,
                    selected_config,
                    enabled=enabled,
                    allow_disabled_legacy=(
                        not enabled
                        and executor_type is None
                        and executor_config is None
                        and selected_type in {"python", "javascript"}
                    ),
                )
            except CommandConfigError as exc:
                raise BotCommandValidationError(str(exc)) from exc
            try:
                encoded_executor_config = json.dumps(
                    selected_config, ensure_ascii=False, separators=(",", ":")
                )
            except (TypeError, ValueError) as exc:
                raise BotCommandValidationError("执行器配置必须是合法 JSON") from exc
            if len(encoded_executor_config.encode("utf-8")) > _MAX_EXECUTOR_CONFIG_BYTES:
                raise BotCommandValidationError("执行器配置不能超过 32KB")
        else:
            selected_type = None
            encoded_executor_config = None
        item = self.database.upsert_bot_command_config(
            command,
            description,
            enabled,
            json.dumps(roles, ensure_ascii=False),
            menu_visible=menu_visible,
            command_type="system"
            if command in {name for name, _ in DEFAULT_BOT_COMMANDS}
            else None,
            executor_type=selected_type,
            executor_config_json=encoded_executor_config,
        )
        return self._command_item(item, roles=roles)

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
            validate_executor_config(executor_type, config, enabled=enabled)
        except CommandConfigError as exc:
            raise BotCommandValidationError(str(exc)) from exc
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
    def _item_executor_type(item: Any) -> str:
        value = getattr(item, "executor_type", "none") if item is not None else "none"
        return value if value in _EXECUTOR_TYPES else "none"

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
    def _executor_error(
        cls, item: Any, executor_type: str, executor_config: object
    ) -> str | None:
        if item is None:
            return None
        return executor_config_error(
            executor_type,
            executor_config,
            enabled=bool(getattr(item, "enabled", False)),
        )

    @classmethod
    def _command_item(cls, item: Any, *, roles: list[str] | None = None) -> dict[str, Any]:
        executor_type = cls._item_executor_type(item)
        executor_config = cls._item_executor_config(item)
        is_custom = (
            getattr(item, "command_type", "custom") != "system"
            and item.command not in {name for name, _ in DEFAULT_BOT_COMMANDS}
        )
        executor_error = cls._executor_error(item, executor_type, executor_config) if is_custom else None
        return {
            "command": item.command,
            "type": getattr(item, "command_type", "custom")
            if getattr(item, "command_type", "custom") in _COMMAND_TYPES
            else "custom",
            "description": item.description,
            "enabled": bool(item.enabled) and executor_error is None,
            "menuVisible": getattr(item, "menu_visible", item.enabled),
            "allowedRoles": roles if roles is not None else cls._item_roles(item.command, item),
            "executorType": executor_type,
            "executorConfig": executor_config,
            "executorError": executor_error,
            "sortOrder": getattr(item, "sort_order", None),
            "updatedAt": utc_isoformat(getattr(item, "updated_at", None)),
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
