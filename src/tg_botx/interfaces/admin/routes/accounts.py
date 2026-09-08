from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, Response

from tg_botx.config import Settings
from tg_botx.features.accounts.service import LoginFlowManager
from tg_botx.infrastructure.persistence.db import (
    Database,
)
from tg_botx.interfaces.admin.admin_security import (
    TransportKeyManager,
)
from tg_botx.interfaces.admin.errors import APIError
from tg_botx.interfaces.admin.models import (
    EmptyBody,
    EncryptedBody,
    LoginStartBody,
    MessageProbeBody,
)
from tg_botx.interfaces.admin.presenters import _account_json, _flow_json, _iso

logger = logging.getLogger(__name__)


def build_router(
    settings: Settings, database: Database, keys: TransportKeyManager, accounts: LoginFlowManager
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/accounts")
    async def list_accounts() -> dict[str, Any]:
        items = accounts.list_accounts()
        return {"items": [_account_json(item, database) for item in items], "total": len(items)}

    @router.get("/api/accounts/{account_id}/chats")
    async def list_account_chats(
        account_id: str,
        chat_type: str = Query("all", alias="type"),
        query: str | None = Query(None, max_length=100),
        limit: int = Query(200, ge=1, le=500),
    ) -> dict[str, Any]:
        items = await accounts.list_chats(
            account_id,
            chat_type=chat_type,  # type: ignore[arg-type]
            query=query,
            limit=limit,
        )
        return {
            "items": [
                {
                    "id": item.chat_id,
                    "type": item.chat_type,
                    "title": item.title,
                    "username": item.username,
                    "hasAvatar": item.has_avatar,
                    "avatarUrl": (
                        f"/api/avatar/{item.chat_id}/{item.avatar_photo_id}"
                        if item.has_avatar and item.avatar_photo_id is not None
                        else None
                    ),
                }
                for item in items
            ],
            "total": len(items),
        }

    @router.post("/api/accounts/{account_id}/chats/pull")
    async def pull_account_chats(account_id: str, _: EmptyBody) -> dict[str, Any]:
        result = await accounts.pull_chats(account_id)
        return {
            "accountId": result.account_id,
            "added": result.added,
            "updated": result.updated,
            "removed": result.removed,
            "total": result.total,
            "syncedAt": _iso(result.synced_at),
        }

    @router.post("/api/accounts/{account_id}/messages/probe")
    async def probe_account_message(
        account_id: str,
        body: MessageProbeBody,
    ) -> dict[str, Any]:
        result = await accounts.probe_message(
            account_id,
            body.target,
            body.text,
            timeout_seconds=body.timeout_seconds,
        )
        return {
            "messageId": result.message_id,
            "text": result.text,
            "buttons": list(result.buttons),
        }

    @router.get("/api/avatar/{chat_id}/{avatar_photo_id}")
    async def chat_avatar(chat_id: int, avatar_photo_id: int) -> Response:
        path = settings.data_dir / "cache" / "avatars" / f"{chat_id}-{avatar_photo_id}.jpg"
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return Response(status_code=404)
        except OSError:
            return Response(status_code=404)
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=300"},
        )

    @router.post("/api/accounts/login-flows", status_code=201)
    async def start_login(body: LoginStartBody) -> dict[str, Any]:
        return _flow_json(await accounts.start(body.account_name, body.method))

    @router.get("/api/accounts/login-flows/{flow_id}")
    async def get_login(flow_id: str) -> dict[str, Any]:
        return _flow_json(await accounts.get_flow(flow_id))

    def decrypt_sensitive(body: EncryptedBody, purpose: str) -> str:
        payload = keys.decrypt_payload(body.key_id, body.ciphertext, purpose)
        value = payload.get("value")
        if not isinstance(value, str) or not value:
            raise APIError("VALIDATION_FAILED", "敏感输入无效", 422)
        return value

    @router.post("/api/accounts/login-flows/{flow_id}/phone")
    async def submit_phone(flow_id: str, body: EncryptedBody) -> dict[str, Any]:
        value = decrypt_sensitive(body, "phone")
        try:
            return _flow_json(await accounts.submit_phone(flow_id, value))
        finally:
            value = ""

    @router.post("/api/accounts/login-flows/{flow_id}/code")
    async def submit_code(flow_id: str, body: EncryptedBody) -> dict[str, Any]:
        value = decrypt_sensitive(body, "code")
        try:
            return _flow_json(await accounts.submit_code(flow_id, value))
        finally:
            value = ""

    @router.post("/api/accounts/login-flows/{flow_id}/password")
    async def submit_password(flow_id: str, body: EncryptedBody) -> dict[str, Any]:
        value = decrypt_sensitive(body, "password")
        try:
            return _flow_json(await accounts.submit_password(flow_id, value))
        finally:
            value = ""

    @router.delete("/api/accounts/login-flows/{flow_id}", status_code=204)
    async def cancel_login(flow_id: str) -> Response:
        await accounts.cancel(flow_id)
        return Response(status_code=204)

    @router.get("/api/accounts/{account_id}/logout-impact")
    async def logout_impact(account_id: str) -> dict[str, Any]:
        impact = accounts.logout_impact(account_id)
        return {
            "accountId": impact.account_id,
            "tasks": [
                {
                    "id": item.task_id,
                    "name": item.name,
                    "enabled": item.enabled,
                    "archived": item.archived,
                }
                for item in impact.tasks
            ],
            "enabledTaskCount": len(impact.enabled_task_ids),
            "canLogout": not impact.enabled_task_ids,
        }

    @router.post("/api/accounts/{account_id}/logout")
    async def logout_account(account_id: str, _: EmptyBody) -> dict[str, Any]:
        impact = await accounts.logout(account_id)
        return {
            "accountId": impact.account_id,
            "loggedOut": True,
            "tasks": [
                {
                    "id": item.task_id,
                    "name": item.name,
                    "enabled": item.enabled,
                    "archived": item.archived,
                }
                for item in impact.tasks
            ],
        }

    return router
