from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError

from tg_botx.core.time import utc_isoformat
from tg_botx.features.bot.executors.base import ExecutionError, json_bytes
from tg_botx.features.bot.executors.builtin import BUILTINS
from tg_botx.features.bot.executors.registry import ExecutionStatus, ExecutorRegistry
from tg_botx.features.bot.executors.schemas import code_hash
from tg_botx.features.bot.models import (
    _ALL_COMMAND_ROLES,
    _COMMAND_NAME_PATTERN,
    _DEFAULT_COMMAND_ROLES,
    _EXECUTOR_TYPES,
    DEFAULT_BOT_COMMANDS,
    BotBindingError,
    BotCommandConflictError,
    BotCommandForbiddenError,
    BotCommandValidationError,
)
from tg_botx.infrastructure.persistence.db import Database

_DEFAULTS = dict(DEFAULT_BOT_COMMANDS)


class BotCommandService:
    def __init__(self, database: Database, registry: ExecutorRegistry | None = None):
        self.database = database
        self.registry = registry if registry is not None else ExecutorRegistry()

    def command_configs(self) -> list[dict[str, Any]]:
        stored = {item.command: item for item in self.database.list_bot_command_configs()}
        configs: list[dict[str, Any]] = []
        for index, (command, description) in enumerate(DEFAULT_BOT_COMMANDS):
            if command == "checkin" and not hasattr(self.database, "checkin_bot_user"):
                continue
            item = stored.get(command)
            config: dict[str, Any] = (
                self._command_item(item)
                if item is not None
                else {
                    "command": command,
                    "description": description,
                    "enabled": True,
                    "menuVisible": True,
                    "allowedRoles": list(_DEFAULT_COMMAND_ROLES[command]),
                    "sortOrder": index,
                    "updatedAt": None,
                    "revision": 0,
                }
            )
            config.update(type="system", executorType="none", executorConfig={})
            if config.get("sortOrder") is None:
                config["sortOrder"] = index
            configs.append(self._with_status(config, item))
        for command, item in stored.items():
            if command not in _DEFAULTS and _COMMAND_NAME_PATTERN.fullmatch(command):
                config = self._command_item(item)
                config["type"] = "custom"  # Do not trust a legacy/custom row claiming to be system.
                configs.append(self._with_status(config, item))
        return sorted(
            configs,
            key=lambda item: (
                item["sortOrder"] if item["sortOrder"] is not None else len(DEFAULT_BOT_COMMANDS),
                item["command"],
            ),
        )

    def _with_status(self, config: dict[str, Any], stored: Any = None) -> dict[str, Any]:
        config = dict(config)
        kind = config.get("executorType", "none")
        confirmation = getattr(stored, "confirmed_code_hash", None)
        current_hash = code_hash(config["executorConfig"]) if kind == "python" else None
        status = (
            ExecutionStatus("ready")
            if config["type"] == "system"
            else self.registry.status(
                kind,
                config["executorConfig"],
                confirmation,
            )
        )
        raw = getattr(stored, "executor_config_json", None)
        if config["type"] == "custom" and raw is not None and kind not in {"javascript", "none"}:
            try:
                if not isinstance(json.loads(raw), dict):
                    raise ValueError("invalid JSON object")
            except (ValueError, TypeError):
                status = ExecutionStatus(
                    "invalid_config", "历史配置不是合法 JSON 对象", "INVALID_EXECUTOR_CONFIG"
                )
        roles = list(config["allowedRoles"])
        if kind == "builtin_function":
            function = config["executorConfig"].get("function")
            spec = BUILTINS.get(function) if isinstance(function, str) else None
            roles = [role for role in roles if spec is not None and role in spec.roles]
            if not roles and status.state == "ready":
                status = ExecutionStatus(
                    "blocked_by_policy", "命令角色与内置函数权限无交集", "EXECUTION_FORBIDDEN"
                )
        if config["type"] == "custom" and config["command"] == "task":
            status = ExecutionStatus(
                "invalid_config",
                "task 是系统命令 tasks 的保留别名，请重命名",
                "INVALID_EXECUTOR_CONFIG",
            )
        config.update(
            executionStatus=status.state,
            unavailableReason=status.reason,
            executionErrorCode=status.code,
            effectiveEnabled=bool(config["enabled"] and status.state == "ready"),
            effectiveAllowedRoles=roles,
            codeHash=current_hash,
            codeConfirmed=bool(current_hash and confirmation == current_hash),
        )
        return config

    @staticmethod
    def _name(value: str) -> str:
        normalized = value.strip().casefold().removeprefix("/")
        if not _COMMAND_NAME_PATTERN.fullmatch(normalized):
            raise BotCommandValidationError("不支持该管理 Bot 指令")
        return normalized

    def _transform(
        self, command: str, changes: dict[str, Any], current: Any, *, create: bool
    ) -> dict[str, Any]:
        system = command in _DEFAULTS
        if create and (system or current is not None or command == "task"):
            raise BotCommandConflictError("该管理 Bot 指令已存在或为保留名称")
        if current is None and not system and not create:
            raise BotCommandValidationError("指令不存在")
        if "expectedRevision" in changes and changes["expectedRevision"] != getattr(
            current, "revision", 0
        ):
            raise BotCommandConflictError("指令已被修改，请刷新后重试")
        old_type = "none" if system else getattr(current, "executor_type", "none")
        old_config = {} if system else self._item_executor_config(current)
        old_enabled = getattr(current, "enabled", system)
        target = self._name(changes.get("command", command))
        if target != command:
            if system:
                raise BotCommandForbiddenError("系统指令不可修改指令名")
            if target in _DEFAULTS or target == "task":
                raise BotCommandConflictError("该管理 Bot 指令已存在或为保留名称")
        description = changes.get(
            "description", getattr(current, "description", _DEFAULTS.get(command, ""))
        )
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 256:
            raise BotCommandValidationError("指令说明不能为空且不能超过 256 个字符")
        roles = self._normalize_roles(
            changes.get("allowedRoles", self._item_roles(command, current))
        )
        kind = changes.get("executorType", old_type)
        config = changes.get("executorConfig", old_config)
        enabled = changes.get("enabled", old_enabled)
        menu_visible = changes.get("menuVisible", getattr(current, "menu_visible", system))
        if (
            type(enabled) is not bool
            or type(menu_visible) is not bool
            or not isinstance(config, dict)
        ):
            raise BotCommandValidationError("启用/菜单状态必须是布尔值，执行器配置必须是 JSON 对象")
        if system and (kind != "none" or config or "confirmCodeHash" in changes):
            raise BotCommandForbiddenError("系统指令不可配置自定义执行器")
        if kind != old_type and "executorConfig" not in changes:
            raise BotCommandValidationError("切换执行器类型时必须提交完整 executorConfig")
        confirmation = getattr(current, "confirmed_code_hash", None)
        if kind == "python":
            new_hash = code_hash(config)
            changed_code = old_type != kind or code_hash(old_config) != new_hash
            if changed_code:
                confirmation = None
                if "enabled" not in changes:
                    enabled = False
            if "confirmCodeHash" in changes:
                if changes["confirmCodeHash"] != new_hash or new_hash is None:
                    raise BotCommandValidationError("源码确认哈希与当前 Python 脚本不一致")
                confirmation = new_hash
        else:
            confirmation = None
            if "confirmCodeHash" in changes:
                raise BotCommandValidationError("仅 Python 执行器接受源码确认")
        preserve_raw = (
            not create
            and not system
            and not enabled
            and current is not None
            and "executorType" not in changes
            and "executorConfig" not in changes
        )
        try:
            if preserve_raw:
                pass  # Disabling/editing metadata must not rewrite a corrupt/retired script.
            elif kind not in _EXECUTOR_TYPES:
                # Retired/unknown records remain inspectable and can be renamed or
                # disabled, but cannot receive new executor writes or be enabled.
                if create or enabled or "executorType" in changes or "executorConfig" in changes:
                    raise BotCommandValidationError("该执行器已移除或不受支持，请更换执行器")
            else:
                config = self.registry.normalize(kind, config, draft=not enabled)
            if not system and enabled:
                state = self.registry.status(kind, config, confirmation)
                if state.state != "ready":
                    raise BotCommandValidationError(state.reason or "执行器不可用")
                if kind == "builtin_function" and not set(roles).intersection(
                    BUILTINS[config["function"]].roles
                ):
                    raise BotCommandValidationError("命令角色与内置函数权限无交集")
            encoded = (
                getattr(current, "executor_config_json", "{}")
                if preserve_raw
                else json_bytes(config).decode()
            )
        except (ValueError, ExecutionError) as exc:
            raise BotCommandValidationError(str(exc)) from exc
        return {
            "command": target,
            "description": description.strip(),
            "enabled": enabled,
            "menu_visible": menu_visible,
            "allowed_roles_json": json.dumps(roles),
            "command_type": "system" if system else "custom",
            "executor_type": kind,
            "executor_config_json": encoded,
            "confirmed_code_hash": confirmation,
        }

    def _save(
        self, command: str, changes: dict[str, Any], *, create: bool = False
    ) -> dict[str, Any]:
        command = self._name(command)
        try:
            if hasattr(self.database, "mutate_bot_command_config"):
                item = self.database.mutate_bot_command_config(
                    command,
                    lambda current: self._transform(command, changes, current, create=create),
                )
            else:
                # Retain the small pre-existing storage adapter contract for creating
                # disabled `none` commands. Executable/rename writes require transactions.
                current = next(
                    (
                        row
                        for row in self.database.list_bot_command_configs()
                        if row.command == command
                    ),
                    None,
                )
                values = self._transform(command, changes, current, create=create)
                if values["command"] != command or values["executor_type"] != "none":
                    raise BotCommandValidationError("存储适配器不支持原子执行器配置更新")
                item = self.database.upsert_bot_command_config(
                    command,
                    values["description"],
                    values["enabled"],
                    values["allowed_roles_json"],
                    menu_visible=values["menu_visible"],
                    command_type=values["command_type"],
                    executor_type=values["executor_type"],
                    executor_config_json=values["executor_config_json"],
                )
        except IntegrityError as exc:
            raise BotCommandConflictError("该管理 Bot 指令已存在") from exc
        return self._with_status(self._command_item(item), item)

    def patch_command_config(self, command: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self._save(command, changes)

    def update_command_config(
        self,
        command: str,
        description: str,
        enabled: bool,
        allowed_roles: Sequence[str] | None = None,
        menu_visible: bool | None = None,
        new_command: str | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {"description": description, "enabled": enabled}
        if allowed_roles is not None:
            changes["allowedRoles"] = allowed_roles
        if menu_visible is not None:
            changes["menuVisible"] = menu_visible
        if new_command is not None:
            changes["command"] = new_command
        return self._save(command, changes)

    def create_command_config(
        self,
        command: str,
        description: str,
        enabled: bool = False,
        allowed_roles: Sequence[str] | None = None,
        menu_visible: bool = False,
        executor_type: str = "none",
        executor_config: dict[str, Any] | None = None,
        *,
        confirm_code_hash: str | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {
            "description": description,
            "enabled": enabled,
            "allowedRoles": allowed_roles or [],
            "menuVisible": menu_visible,
            "executorType": executor_type,
            "executorConfig": executor_config if executor_config is not None else {},
        }
        if confirm_code_hash is not None:
            changes["confirmCodeHash"] = confirm_code_hash
        return self._save(command, changes, create=True)

    def reorder_command_configs(self, commands: list[str]) -> list[dict[str, Any]]:
        current = self.command_configs()
        normalized = [self._name(name) for name in commands]
        if len(normalized) != len(set(normalized)) or set(normalized) != {
            item["command"] for item in current
        }:
            raise BotCommandValidationError("指令排序列表与当前指令不一致")
        by_name = {item["command"]: item for item in current}
        stored_names = {item.command for item in self.database.list_bot_command_configs()}
        for index, command in enumerate(normalized):
            if command not in stored_names:
                item = by_name[command]
                self.update_command_config(
                    command,
                    item["description"],
                    item["enabled"],
                    item["allowedRoles"],
                    item["menuVisible"],
                )
            if not self.database.set_bot_command_order(command, index):
                raise BotCommandValidationError("指令排序保存失败")
        return self.command_configs()

    @staticmethod
    def _normalize_roles(value: object) -> list[str]:
        if not isinstance(value, (list, tuple, set)) or any(
            not isinstance(role, str) or role not in _ALL_COMMAND_ROLES for role in value
        ):
            raise BotCommandValidationError("可调用身份只能选择未绑定用户、普通用户或管理员")
        return [role for role in _ALL_COMMAND_ROLES if not value or role in value]

    @staticmethod
    def _item_executor_config(item: Any) -> dict[str, Any]:
        raw = getattr(item, "executor_config_json", None)
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    return value
            except (ValueError, TypeError):
                pass
        return {}

    @classmethod
    def _command_item(cls, item: Any, *, roles: list[str] | None = None) -> dict[str, Any]:
        return {
            "command": item.command,
            "type": "system" if item.command in _DEFAULTS else "custom",
            "description": item.description,
            "enabled": item.enabled,
            "menuVisible": getattr(item, "menu_visible", item.enabled),
            "allowedRoles": roles if roles is not None else cls._item_roles(item.command, item),
            "executorType": getattr(item, "executor_type", "none"),
            "executorConfig": cls._item_executor_config(item),
            "revision": getattr(item, "revision", 0),
            "sortOrder": getattr(item, "sort_order", None),
            "updatedAt": utc_isoformat(getattr(item, "updated_at", None)),
        }

    @staticmethod
    def _menu_visible(item: dict[str, Any]) -> bool:
        return bool(
            item.get("menuVisible", item.get("enabled", False))
            and item.get("effectiveEnabled", item.get("enabled", False))
        )

    @classmethod
    def _item_roles(cls, command: str, item: Any) -> list[str]:
        raw = getattr(item, "allowed_roles_json", None) if item is not None else None
        if isinstance(raw, str):
            try:
                return cls._normalize_roles(json.loads(raw))
            except (ValueError, TypeError, BotBindingError):
                pass
        if item is not None and raw is None:
            return list(_ALL_COMMAND_ROLES)
        return list(_DEFAULT_COMMAND_ROLES.get(command, _ALL_COMMAND_ROLES))

    def delete_command_config(self, command: str) -> bool:
        command = self._name(command)
        if command in _DEFAULTS:
            raise BotCommandForbiddenError("系统指令不可删除")
        return self.database.delete_bot_command_config(command)
