from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tg_botx.config import Settings
from tg_botx.features.bot.models import BotBindingError
from tg_botx.infrastructure.persistence.db import (
    Database,
)
from tg_botx.interfaces.admin.errors import APIError
from tg_botx.interfaces.admin.models import BotAdminBindingBody, BotBindingBatchBody, EmptyBody
from tg_botx.interfaces.admin.presenters import _iso
from tg_botx.interfaces.telegram.runtime import TelegramManagementBot

logger = logging.getLogger(__name__)


def build_router(
    settings: Settings, database: Database, admin_bot: TelegramManagementBot
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/bot/bindings", status_code=201)
    async def create_bot_binding(_: EmptyBody) -> dict[str, Any]:
        code, item = admin_bot.management.create_binding_code()
        admin_bot.management.audit(
            None,
            None,
            "binding_code_create",
            "success",
            details=json.dumps({"role": "user", "quantity": 1, "ttlDays": 0}, ensure_ascii=False),
        )
        return {
            "id": item.id,
            "code": code,
            "createdAt": _iso(item.created_at),
            "expiresAt": None
            if item.expires_at is not None and item.expires_at.year >= 9999
            else _iso(item.expires_at),
            "role": item.role or "user",
        }

    @router.post("/api/bot/binding-codes/batch", status_code=201)
    async def create_bot_binding_batch(
        request: Request, body: BotBindingBatchBody
    ) -> dict[str, Any]:
        key = request.headers.get("Idempotency-Key")
        try:
            batch_id, generated = admin_bot.management.create_binding_codes(
                body.quantity, body.ttl_days, role=body.role, idempotency_key=key
            )
        except ValueError as exc:
            if str(exc) == "IDEMPOTENCY_CONFLICT":
                raise APIError("IDEMPOTENCY_CONFLICT", "幂等键已用于其他请求", 409) from exc
            raise APIError("VALIDATION_FAILED", "绑定码参数无效", 400) from exc
        except BotBindingError as exc:
            raise APIError("VALIDATION_FAILED", str(exc), 400) from exc
        admin_bot.management.audit(
            None,
            None,
            "binding_code_batch_create",
            "success",
            details=json.dumps(
                {
                    "role": body.role,
                    "batchId": batch_id,
                    "quantity": body.quantity,
                    "ttlDays": body.ttl_days,
                },
                ensure_ascii=False,
            ),
        )
        return {
            "batchId": batch_id,
            "codes": [
                {
                    "id": item.id,
                    "code": code,
                    "hint": item.code_hint,
                    "role": item.role or body.role,
                    "createdAt": _iso(item.created_at),
                    "expiresAt": None
                    if item.expires_at is not None and item.expires_at.year >= 9999
                    else _iso(item.expires_at),
                }
                for code, item in generated
            ],
        }

    @router.post("/api/bot/binding-codes/admin", status_code=201)
    async def create_admin_bot_binding(
        request: Request, body: BotAdminBindingBody
    ) -> dict[str, Any]:
        try:
            batch_id, generated = admin_bot.management.create_binding_codes(
                1,
                body.ttl_days,
                role="admin",
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        except (BotBindingError, ValueError) as exc:
            code = (
                "IDEMPOTENCY_CONFLICT"
                if str(exc) == "IDEMPOTENCY_CONFLICT"
                else "VALIDATION_FAILED"
            )
            raise APIError(
                code, "管理员绑定码请求无效", 409 if code == "IDEMPOTENCY_CONFLICT" else 400
            ) from exc
        code_value, item = generated[0]
        admin_bot.management.audit(
            None,
            None,
            "binding_code_create",
            "success",
            details=json.dumps(
                {"role": "admin", "quantity": 1, "ttlDays": body.ttl_days}, ensure_ascii=False
            ),
        )
        return {
            "batchId": batch_id,
            "id": item.id,
            "code": code_value,
            "role": "admin",
            "createdAt": _iso(item.created_at),
            "expiresAt": None
            if item.expires_at is not None and item.expires_at.year >= 9999
            else _iso(item.expires_at),
        }

    @router.get("/api/bot/binding-codes")
    async def list_bot_binding_codes(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, alias="pageSize", ge=1, le=100),
    ) -> dict[str, Any]:
        codes, total = admin_bot.management.binding_codes_page(page=page, page_size=page_size)
        return {
            "enabled": settings.bot_enabled,
            "configured": admin_bot.status.configured,
            "status": admin_bot.public_status(),
            "codes": [
                {
                    "id": item.id,
                    "hint": item.hint,
                    "role": item.role,
                    "status": item.status,
                    "createdAt": _iso(item.created_at),
                    "expiresAt": None
                    if item.expires_at is not None and item.expires_at.year >= 9999
                    else _iso(item.expires_at),
                    "usedAt": _iso(item.used_at),
                    "revokedAt": _iso(item.revoked_at),
                }
                for item in codes
            ],
            "codesPagination": {"page": page, "pageSize": page_size, "total": total},
        }

    @router.get("/api/bot/bindings")
    async def list_bot_bindings(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, alias="pageSize", ge=1, le=100),
    ) -> dict[str, Any]:
        bindings, total = admin_bot.management.bindings_page(page=page, page_size=page_size)
        serialized_bindings = []
        for item in bindings:
            points = database.get_bot_user_points(item.user_id)
            serialized_bindings.append(
                {
                    "id": item.id,
                    "userId": item.user_id,
                    "chatId": item.chat_id,
                    "username": item.username,
                    "firstName": item.first_name,
                    "lastName": item.last_name,
                    "boundAt": _iso(item.bound_at),
                    "role": item.role or "user",
                    "points": points.points if points is not None else 0,
                }
            )
        return {
            "enabled": settings.bot_enabled,
            "configured": admin_bot.status.configured,
            "status": admin_bot.public_status(),
            "bindings": serialized_bindings,
            "bindingsPagination": {"page": page, "pageSize": page_size, "total": total},
        }

    @router.delete("/api/bot/binding-codes/{code_id}", status_code=204)
    async def revoke_bot_binding_code(code_id: str) -> Response:
        if not admin_bot.management.revoke_code(code_id):
            raise APIError("NOT_FOUND", "绑定码不存在或已失效", 404)
        return Response(status_code=204)

    @router.delete("/api/bot/bindings/{binding_id}", status_code=204)
    async def revoke_bot_binding(binding_id: str) -> Response:
        if not admin_bot.management.revoke_binding(binding_id):
            raise APIError("NOT_FOUND", "绑定关系不存在或已解除", 404)
        return Response(status_code=204)

    return router
