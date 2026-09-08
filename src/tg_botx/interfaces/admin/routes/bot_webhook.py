from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response

from tg_botx.interfaces.admin.constants import (
    ADMIN_BOT_WEBHOOK_PATH,
)
from tg_botx.interfaces.admin.errors import APIError
from tg_botx.interfaces.telegram.runtime import TelegramManagementBot

logger = logging.getLogger(__name__)


def build_router(admin_bot: TelegramManagementBot) -> APIRouter:
    router = APIRouter()

    @router.post(ADMIN_BOT_WEBHOOK_PATH, include_in_schema=False, status_code=204)
    async def telegram_admin_bot_webhook(request: Request) -> Response:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if not admin_bot.webhook_secret_matches(secret):
            raise APIError("WEBHOOK_FORBIDDEN", "Webhook 请求未通过验证", 403)
        # Snapshot readiness before reading the body. Requests that were
        # already in flight while setWebhook was being registered must be
        # acknowledged and discarded; returning a non-2xx response would make
        # Telegram retry those stale updates after startup completes.
        ready = admin_bot.accepts_webhook(secret)
        try:
            update = await request.json()
        except ValueError as exc:
            raise APIError("WEBHOOK_INVALID", "Webhook 请求体不是有效 JSON", 400) from exc
        if not isinstance(update, dict):
            raise APIError("WEBHOOK_INVALID", "Webhook 请求体必须是 JSON 对象", 400)
        if not ready or not admin_bot.accepts_webhook(secret):
            await admin_bot.discard_webhook_update(update)
            return Response(status_code=204)
        await admin_bot.handle_webhook_update(update)
        return Response(status_code=204)

    return router
