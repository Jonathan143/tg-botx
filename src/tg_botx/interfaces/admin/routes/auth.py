from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from tg_botx.config import Settings
from tg_botx.interfaces.admin.admin_security import (
    FailureRateLimiter,
    SecurityError,
    SessionManager,
    TransportKeyManager,
    resolve_client_ip,
)
from tg_botx.interfaces.admin.constants import (
    SESSION_COOKIE,
)
from tg_botx.interfaces.admin.models import AdminVerifyBody, EmptyBody
from tg_botx.interfaces.admin.presenters import (
    _iso,
)

logger = logging.getLogger(__name__)


def build_router(
    settings: Settings,
    keys: TransportKeyManager,
    sessions: SessionManager,
    limiter: FailureRateLimiter,
    trusted_proxies: list[str],
    admin_key: str | bytes,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/key")
    async def auth_key(
        purpose: Literal["admin", "phone", "code", "password"] = "admin",
    ) -> JSONResponse:
        return JSONResponse(keys.issue_challenge(purpose), headers={"Cache-Control": "no-store"})

    @router.post("/api/auth/verify")
    async def auth_verify(request: Request, body: AdminVerifyBody) -> JSONResponse:
        peer = request.client.host if request.client else "unknown"
        client_ip = resolve_client_ip(peer, request.headers.get("X-Forwarded-For"), trusted_proxies)
        limiter.check(client_ip)
        try:
            keys.verify_admin_payload(body.key_id, body.ciphertext, admin_key, purpose="admin")
        except SecurityError:
            limiter.record_failure(client_ip)
            raise SecurityError("AUTH_FAILED", "管理员身份验证失败", status_code=401) from None
        limiter.record_success(client_ip)
        credentials = sessions.create()
        response = JSONResponse(
            {
                "authenticated": True,
                "csrfToken": credentials.csrf_token,
                "sessionExpiresAt": _iso(credentials.expires_at),
            }
        )
        response.set_cookie(
            SESSION_COOKIE,
            credentials.token,
            max_age=settings.admin_session_days * 86400,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/api",
        )
        return response

    @router.get("/api/auth/session")
    async def auth_session(request: Request) -> dict[str, Any]:
        credentials = request.state.session
        return {
            "authenticated": True,
            "csrfToken": credentials.csrf_token,
            "sessionExpiresAt": _iso(credentials.expires_at),
        }

    @router.post("/api/auth/logout")
    async def auth_logout(request: Request, _: EmptyBody) -> Response:
        sessions.revoke(request.state.session.token)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/api", secure=True, samesite="strict")
        return response

    return router
