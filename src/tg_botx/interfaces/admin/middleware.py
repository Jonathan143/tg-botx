from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from tg_botx.config import Settings
from tg_botx.features.accounts.service import AdminAccountError
from tg_botx.features.checkin.errors import ManualRunConflict, TaskNotFound, TaskStateError
from tg_botx.interfaces.admin.admin_security import (
    SecurityError,
    SessionManager,
)
from tg_botx.interfaces.admin.constants import (
    ADMIN_BOT_WEBHOOK_PATH,
    CSRF_HEADER,
    SESSION_COOKIE,
)
from tg_botx.interfaces.admin.errors import APIError, _validation_details

logger = logging.getLogger(__name__)


def install_middleware(
    app: FastAPI, settings: Settings, sessions: SessionManager, admin_origin: str
) -> None:
    def error_response(request: Request, error: APIError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        payload: dict[str, Any] = {
            "error": {"code": error.code, "message": error.message, "requestId": request_id}
        }
        if error.details is not None:
            payload["error"]["details"] = error.details
        return JSONResponse(payload, status_code=error.status_code, headers=error.headers)

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        return error_response(request, exc)

    @app.exception_handler(TaskNotFound)
    async def handle_task_missing(request: Request, exc: TaskNotFound) -> JSONResponse:
        return error_response(request, APIError("NOT_FOUND", "任务不存在", 404))

    @app.exception_handler(TaskStateError)
    async def handle_task_state(request: Request, exc: TaskStateError) -> JSONResponse:
        return error_response(request, APIError("CONFLICT", str(exc), 409))

    @app.exception_handler(ManualRunConflict)
    async def handle_run_conflict(request: Request, exc: ManualRunConflict) -> JSONResponse:
        return error_response(request, APIError("TASK_BUSY", "同一账号和目标已有任务在执行", 409))

    @app.exception_handler(SecurityError)
    async def handle_security_error(request: Request, exc: SecurityError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return error_response(
            request, APIError(exc.code, exc.message, exc.status_code, headers=headers)
        )

    @app.exception_handler(AdminAccountError)
    async def handle_account_error(request: Request, exc: AdminAccountError) -> JSONResponse:
        status = 404 if exc.code in {"ACCOUNT_NOT_FOUND", "LOGIN_FLOW_NOT_FOUND"} else 409
        return error_response(request, APIError(exc.code, exc.message, status))

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            request,
            APIError("VALIDATION_FAILED", "请求参数无效", 422, details=_validation_details(exc)),
        )

    @app.exception_handler(OperationalError)
    async def handle_database_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
        logger.error(
            "管理 API 数据库不可用 request_id=%s",
            getattr(request.state, "request_id", "-"),
            exc_info=exc,
        )
        return error_response(
            request,
            APIError(
                "DATABASE_UNAVAILABLE",
                "数据库暂时不可用，请稍后重试",
                503,
                headers={"Retry-After": "5"},
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "管理 API 未预期异常 request_id=%s type=%s",
            getattr(request.state, "request_id", "-"),
            type(exc).__name__,
        )
        return error_response(request, APIError("INTERNAL_ERROR", "服务暂时无法处理请求", 500))

    @app.middleware("http")
    async def request_security(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        path = request.url.path
        method = request.method.upper()
        is_key = method == "GET" and path == "/api/auth/key"
        is_verify = method == "POST" and path == "/api/auth/verify"
        is_telegram_webhook = method == "POST" and path == ADMIN_BOT_WEBHOOK_PATH
        mutating = method in {"POST", "PUT", "PATCH", "DELETE"}
        try:
            if mutating and not is_telegram_webhook:
                if request.headers.get("Origin") != admin_origin:
                    raise APIError("ORIGIN_FORBIDDEN", "请求来源不被允许", 403)
                content_type = (
                    request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                )
                if content_type != "application/json":
                    raise APIError("JSON_REQUIRED", "修改请求必须使用 application/json", 415)
            if not is_key and not is_verify and not is_telegram_webhook:
                token = request.cookies.get(SESSION_COOKIE)
                if not token:
                    raise APIError("AUTH_REQUIRED", "需要管理员身份验证", 401)
                try:
                    credentials = sessions.authenticate(
                        token,
                        csrf_token=request.headers.get(CSRF_HEADER),
                        require_csrf=mutating,
                    )
                except SecurityError as exc:
                    if exc.code in {"SESSION_INVALID", "AUTH_FAILED"}:
                        raise APIError("AUTH_REQUIRED", "需要管理员身份验证", 401) from exc
                    raise
                request.state.session = credentials
            response = await call_next(request)
            if (
                not is_key
                and not is_verify
                and not is_telegram_webhook
                and hasattr(request.state, "session")
            ):
                credentials = request.state.session
                response.set_cookie(
                    SESSION_COOKIE,
                    credentials.token,
                    max_age=settings.admin_session_days * 86400,
                    httponly=True,
                    secure=True,
                    samesite="strict",
                    path="/api",
                )
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers.setdefault("Cache-Control", "no-store")
            return response
        except APIError as exc:
            response = error_response(request, exc)
        except SecurityError as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            response = error_response(
                request, APIError(exc.code, exc.message, exc.status_code, headers=headers)
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response
