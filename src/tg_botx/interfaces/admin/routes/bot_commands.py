from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter
from fastapi.responses import Response

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
    EmptyBody,
)
from tg_botx.interfaces.telegram.runtime import TelegramManagementBot

logger = logging.getLogger(__name__)


def build_router(admin_bot: TelegramManagementBot) -> APIRouter:
    router = APIRouter()

    @router.get("/api/bot/commands")
    async def list_bot_commands() -> dict[str, Any]:
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
        try:
            return admin_bot.management.create_command_config(
                body.command,
                description,
                body.enabled,
                body.allowed_roles,
                body.menu_visible,
                body.executor_type,
                body.executor_config,
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
        description = (
            body.description if body.description is not None else current["description"]
        ).strip()
        if not description:
            raise APIError("VALIDATION_FAILED", "指令说明不能为空", 422)
        enabled = body.enabled if body.enabled is not None else current["enabled"]
        menu_visible = (
            body.menu_visible
            if body.menu_visible is not None
            else current.get("menuVisible", current["enabled"])
        )
        try:
            item = admin_bot.management.update_command_config(
                normalized,
                description,
                enabled,
                body.allowed_roles,
                menu_visible,
                body.command,
                body.executor_type,
                body.executor_config,
            )
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

    return router
