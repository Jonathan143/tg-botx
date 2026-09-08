from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response

from tg_botx.config import Settings
from tg_botx.infrastructure.persistence.db import (
    utc_now,
)
from tg_botx.interfaces.admin.admin_security import (
    TransportKeyManager,
)
from tg_botx.interfaces.admin.models import EmptyBody
from tg_botx.interfaces.admin.presenters import (
    _iso,
)
from tg_botx.interfaces.telegram.runtime import TelegramManagementBot

logger = logging.getLogger(__name__)


def build_router(
    settings: Settings,
    keys: TransportKeyManager,
    admin_bot: TelegramManagementBot,
    trusted_proxies: list[str],
    app: FastAPI,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/settings/transport-key/rotate")
    async def rotate_transport_key(_: EmptyBody) -> dict[str, Any]:
        rotated_at = utc_now()
        key_id = keys.rotate_now()
        return {
            "keyId": key_id,
            "rotatedAt": _iso(rotated_at),
            "previousKeyExpiresAt": _iso(
                rotated_at + timedelta(seconds=keys.old_key_grace_seconds)
            ),
        }

    @router.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {
            "database": settings.database,
            "databaseUrl": "[REDACTED]" if settings.database_url_override else None,
            "dataDir": str(settings.data_dir),
            "apiHost": settings.api_host,
            "apiPort": settings.api_port,
            "adminOrigin": settings.admin_origin,
            "sessionDays": settings.admin_session_days,
            "transportKeyRotationHours": settings.transport_key_rotation_hours,
            "trustedProxiesConfigured": bool(trusted_proxies),
            "telegramApiConfigured": bool(settings.api_id and (settings.api_hash or "").strip()),
            "notificationConfigured": bool(
                settings.notification_bot_token
                and settings.notification_bot_token.get_secret_value().strip()
            ),
            "adminBotEnabled": settings.bot_enabled,
            "adminBotConfigured": admin_bot.status.configured,
            "adminBotStatus": admin_bot.public_status(),
            "notificationTimezone": settings.notification_timezone,
            "logLevel": settings.log_level,
            "logFile": settings.log_file,
            "logMaxBytes": settings.log_max_bytes,
            "logBackupCount": settings.log_backup_count,
            "readOnly": True,
        }

    @router.get("/api/openapi.json", include_in_schema=False)
    async def protected_openapi() -> JSONResponse:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        return JSONResponse(schema)

    @router.get("/api/docs", include_in_schema=False)
    async def protected_docs() -> Response:
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json", title=f"{app.title} - Swagger UI"
        )

    return router
