from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import Response

from tg_botx.features.bot.execution import execution_view
from tg_botx.features.bot.executors.base import ExecutionError
from tg_botx.features.bot.executors.builtin import builtin_catalog
from tg_botx.features.bot.executors.schemas import code_hash
from tg_botx.features.bot.models import (
    BotBindingError,
    BotCommandConflictError,
    BotCommandForbiddenError,
    BotCommandValidationError,
)
from tg_botx.interfaces.admin.errors import APIError
from tg_botx.interfaces.admin.models import (
    BotCheckinConfigBody,
    BotCommandBody,
    BotCommandCreateBody,
    BotCommandOrderBody,
    CommandTestBody,
    CommandValidationBody,
    EmptyBody,
)
from tg_botx.interfaces.telegram.runtime import TelegramManagementBot

logger = logging.getLogger(__name__)


def build_router(admin_bot: TelegramManagementBot) -> APIRouter:
    router = APIRouter()

    @router.get("/api/bot/commands")
    async def list_bot_commands() -> dict[str, Any]:
        await admin_bot.executions.refresh_capabilities()
        return {"commands": admin_bot.command_configs()}

    @router.get("/api/bot/checkin-config")
    async def get_bot_checkin_config() -> dict[str, int]:
        return admin_bot.management.checkin_config()

    @router.patch("/api/bot/checkin-config")
    @router.put("/api/bot/checkin-config")
    async def update_bot_checkin_config(body: BotCheckinConfigBody) -> dict[str, int]:
        try:
            return admin_bot.management.update_checkin_config(body.min_points, body.max_points)
        except BotBindingError as exc:
            raise APIError("VALIDATION_FAILED", str(exc), 422) from exc

    @router.post("/api/bot/commands", status_code=201)
    async def create_bot_command(body: BotCommandCreateBody) -> dict[str, Any]:
        description = body.description.strip()
        if not description:
            raise APIError("VALIDATION_FAILED", "指令说明不能为空", 422)
        await admin_bot.executions.refresh_capabilities()
        try:
            return admin_bot.management.create_command_config(
                body.command,
                description,
                body.enabled,
                body.allowed_roles,
                body.menu_visible,
                body.executor_type,
                body.executor_config,
                confirm_code_hash=body.confirm_code_hash,
            )
        except BotCommandConflictError as exc:
            raise APIError("COMMAND_CONFLICT", str(exc), 409) from exc
        except (BotCommandValidationError, ValueError) as exc:
            raise APIError("VALIDATION_FAILED", str(exc), 422) from exc

    @router.post("/api/bot/commands/pull")
    async def pull_bot_commands(_: EmptyBody) -> dict[str, Any]:
        if admin_bot.client is None:
            raise APIError("BOT_NOT_CONFIGURED", "Telegram 管理 Bot 未配置", 503)
        try:
            commands = await admin_bot.pull_remote_commands()
        except Exception as exc:
            logger.warning("手动拉取管理 Bot 指令失败 type=%s", type(exc).__name__)
            raise APIError("COMMAND_PULL_FAILED", "从 Telegram 拉取指令失败", 502) from exc
        return {"commands": commands}

    @router.put("/api/bot/commands/order")
    async def reorder_bot_commands(body: BotCommandOrderBody) -> dict[str, Any]:
        try:
            return {"commands": admin_bot.management.reorder_command_configs(body.commands)}
        except (BotCommandValidationError, ValueError) as exc:
            raise APIError("VALIDATION_FAILED", str(exc), 422) from exc

    @router.post("/api/bot/commands/sync")
    async def sync_bot_commands(_: EmptyBody) -> dict[str, Any]:
        if admin_bot.client is None:
            raise APIError("BOT_NOT_CONFIGURED", "Telegram 管理 Bot 未配置", 503)
        try:
            await admin_bot.refresh_commands()
        except Exception as exc:
            logger.warning("手动同步管理 Bot 指令失败 type=%s", type(exc).__name__)
            raise APIError("COMMAND_SYNC_FAILED", "向 Telegram 同步指令失败", 502) from exc
        return {"commands": admin_bot.command_configs()}

    @router.patch("/api/bot/commands/{command}")
    @router.put("/api/bot/commands/{command}")
    async def update_bot_command(command: str, body: BotCommandBody) -> dict[str, Any]:
        normalized = command.casefold().removeprefix("/")
        if not re.fullmatch(r"^[a-z0-9_]{1,32}$", normalized):
            raise APIError("VALIDATION_FAILED", "不支持该管理 Bot 指令", 422)
        current = next(
            (
                item
                for item in admin_bot.management.command_configs()
                if item["command"] == normalized
            ),
            None,
        )
        if current is None:
            raise APIError("COMMAND_NOT_FOUND", "不支持该管理 Bot 指令", 404)
        await admin_bot.executions.refresh_capabilities()
        changes = body.model_dump(by_alias=True, exclude_unset=True)
        if any(value is None for value in changes.values()):
            raise APIError("VALIDATION_FAILED", "PATCH 字段不能为 null；未修改的字段应省略", 422)
        try:
            item = admin_bot.management.commands.patch_command_config(normalized, changes)
        except BotCommandConflictError as exc:
            raise APIError("COMMAND_CONFLICT", str(exc), 409) from exc
        except BotCommandForbiddenError as exc:
            raise APIError("COMMAND_FORBIDDEN", str(exc), 403) from exc
        except (BotCommandValidationError, ValueError) as exc:
            raise APIError("VALIDATION_FAILED", str(exc), 422) from exc
        except BotBindingError as exc:
            raise APIError("VALIDATION_FAILED", str(exc), 422) from exc
        return item

    @router.delete("/api/bot/commands/{command}", status_code=204)
    async def delete_bot_command(command: str) -> Response:
        normalized = command.casefold().removeprefix("/")
        if not re.fullmatch(r"^[a-z0-9_]{1,32}$", normalized):
            raise APIError("VALIDATION_FAILED", "不支持该管理 Bot 指令", 422)
        current = next(
            (
                item
                for item in admin_bot.management.command_configs()
                if item["command"] == normalized
            ),
            None,
        )
        if current is None:
            raise APIError("COMMAND_NOT_FOUND", "不支持该管理 Bot 指令", 404)
        try:
            admin_bot.management.delete_command_config(normalized)
        except BotCommandForbiddenError as exc:
            raise APIError("COMMAND_FORBIDDEN", str(exc), 403) from exc
        except BotBindingError as exc:
            raise APIError("VALIDATION_FAILED", str(exc), 422) from exc
        return Response(status_code=204)

    @router.get("/api/bot/executors")
    async def executor_capabilities() -> dict[str, Any]:
        service = admin_bot.executions
        await service.refresh_capabilities()
        policy = service.registry.policy
        return {
            "executors": service.registry.catalog(),
            "removedTypes": ["javascript"],
            "templateVariables": ["argument", "command", "user.id", "user.role", "chat.id"],
            "allowedHttpOrigins": sorted(policy.allowed_origins),
            "credentials": [
                {"ref": name, "origin": value.origin} for name, value in policy.credentials.items()
            ],
            "limits": {
                "configBytes": 32768,
                "replyUtf16Units": 3500,
                "workers": policy.max_workers,
                "pythonWorkers": policy.python_workers,
                "queueLimit": policy.queue_limit,
                "queueSeconds": policy.queue_seconds,
                "callsPerActorPerMinute": policy.rate_limit,
                "retentionDays": policy.retention_days,
            },
        }

    @router.get("/api/bot/builtin-functions")
    async def list_builtin_functions() -> dict[str, Any]:
        return {"functions": builtin_catalog()}

    @router.post("/api/bot/command-validation")
    async def validate_command(body: CommandValidationBody) -> dict[str, Any]:
        # No network calls and no code execution: this endpoint is a pure validator.
        registry = admin_bot.executions.registry
        try:
            normalized = registry.normalize(body.executor_type, body.executor_config)
        except (ValueError, ExecutionError) as exc:
            return {"valid": False, "canEnable": False, "errors": [str(exc)]}
        status = registry.status(body.executor_type, normalized, body.confirm_code_hash)
        return {
            "valid": True,
            "canEnable": status.state == "ready",
            "errors": [],
            "executorConfig": normalized,
            "executionStatus": status.state,
            "unavailableReason": status.reason,
            "codeHash": code_hash(normalized) if body.executor_type == "python" else None,
        }

    @router.post("/api/bot/command-tests", status_code=202)
    async def test_command(
        body: CommandTestBody, idempotency_key: str | None = Header(default=None, max_length=128)
    ) -> dict[str, Any]:
        if body.executor_type in {"http", "python"} and not body.confirm_execution:
            raise APIError(
                "EXECUTION_CONFIRMATION_REQUIRED", "真实试运行需要 confirmExecution=true", 422
            )
        service = admin_bot.executions
        await service.refresh_capabilities()
        try:
            item, created = service.submit_test(
                body.executor_type,
                body.executor_config,
                body.argument,
                confirmation=body.confirm_code_hash,
                idempotency_key=idempotency_key,
            )
        except ExecutionError as exc:
            status = (
                429
                if exc.code in {"EXECUTION_BUSY", "EXECUTION_RATE_LIMITED"}
                else 503
                if exc.code in {"EXECUTOR_UNAVAILABLE", "SERVICE_STOPPING"}
                else 409
                if exc.code == "IDEMPOTENCY_CONFLICT"
                else 422
            )
            raise APIError(exc.code, str(exc), status) from exc
        except ValueError as exc:
            raise APIError("INVALID_EXECUTOR_CONFIG", str(exc), 422) from exc
        return {**execution_view(item), "created": created}

    @router.get("/api/bot/command-executions")
    async def list_executions(
        command: str | None = Query(default=None, pattern=r"^[a-z0-9_]{1,32}$"),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        service = admin_bot.executions
        return {
            "executions": [
                execution_view(item)
                for item in service.repository.list_recent(
                    service.bot_identity, command=command, limit=limit
                )
            ]
        }

    @router.get("/api/bot/command-executions/{execution_id}")
    async def get_execution(execution_id: str) -> dict[str, Any]:
        service = admin_bot.executions
        item = service.repository.get(execution_id, service.bot_identity)
        if item is None:
            raise APIError("EXECUTION_NOT_FOUND", "执行记录不存在", 404)
        return execution_view(item)

    return router
