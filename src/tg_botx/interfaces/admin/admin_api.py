from __future__ import annotations

import asyncio
import copy
import io
import ipaddress
import json
import logging
import re
import signal
import threading
import time
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import yaml
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import OperationalError

from tg_botx.config import Settings
from tg_botx.features.admin_bot import (
    BotBindingError,
    BotCommandConflictError,
    BotCommandForbiddenError,
    BotCommandValidationError,
    TelegramManagementBot,
)
from tg_botx.features.checkin.runtime import (
    CheckinService,
    ManualRunConflict,
    TaskNotFound,
    TaskStateError,
)
from tg_botx.features.checkin.schedule import next_runs, schedule_from_task
from tg_botx.infrastructure.observability.logging import allowed_log_files, redact_sensitive
from tg_botx.infrastructure.persistence.db import (
    Database,
    Task,
    TaskRun,
    WorkflowVersion,
    utc_isoformat,
    utc_now,
)
from tg_botx.interfaces.admin.admin_accounts import AdminAccountError, LoginFlowManager
from tg_botx.interfaces.admin.admin_security import (
    FailureRateLimiter,
    SecurityError,
    SessionManager,
    TransportKeyManager,
    resolve_client_ip,
)
from tg_botx.schemas import TaskDefinition

logger = logging.getLogger(__name__)

SESSION_COOKIE = "tg_bot_admin_session"
CSRF_HEADER = "X-CSRF-Token"
ADMIN_BOT_WEBHOOK_PATH = "/api/telegram/admin-bot/webhook"
TASK_EVENT_KEEPALIVE_SECONDS = 15
LOG_EVENT_POLL_SECONDS = 1
_LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T][^ ]+)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>\S+)\s*(?P<message>.*)$"
)


def _install_shutdown_signal_handlers(
    shutdown_event: asyncio.Event,
) -> dict[signal.Signals, Any]:
    """Notify streaming responses before Uvicorn starts graceful shutdown."""
    if threading.current_thread() is not threading.main_thread():
        return {}

    previous_handlers: dict[signal.Signals, Any] = {}
    for received in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(received)
        if not callable(previous):
            continue

        def handle_shutdown(signum: int, frame: Any, previous: Any = previous) -> None:
            shutdown_event.set()
            previous(signum, frame)

        signal.signal(received, handle_shutdown)
        previous_handlers[received] = previous
    return previous_handlers


def _restore_signal_handlers(previous_handlers: dict[signal.Signals, Any]) -> None:
    for received, previous in previous_handlers.items():
        signal.signal(received, previous)


class APIError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        details: Any | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        self.headers = headers or {}


class AdminVerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str = Field(alias="keyId")
    ciphertext: str


class TaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: TaskDefinition


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    yaml: str = Field(min_length=1, max_length=1_000_000)
    overwrite_names: list[str] = Field(default_factory=list, alias="overwriteNames")


class LoginStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_name: str = Field(alias="accountName", min_length=1, max_length=100)
    method: Literal["qr", "phone"]


class EncryptedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str = Field(alias="keyId")
    ciphertext: str


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BotBindingBatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user"] = "user"
    quantity: int = Field(default=1, ge=1, le=100)
    ttl_days: int | None = Field(default=1, alias="ttlDays")


class BotAdminBindingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_days: int | None = Field(default=1, alias="ttlDays")


class BotCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, max_length=256)
    command: str | None = Field(default=None, min_length=1, max_length=32)
    enabled: bool | None = None
    menu_visible: bool | None = Field(default=None, alias="menuVisible")
    allowed_roles: list[Literal["anonymous", "user", "admin"]] | None = Field(
        default=None, alias="allowedRoles", max_length=3
    )


class BotCommandCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=256)
    enabled: bool = False
    menu_visible: bool = Field(default=False, alias="menuVisible")
    allowed_roles: list[Literal["anonymous", "user", "admin"]] = Field(
        default_factory=list, alias="allowedRoles", max_length=3
    )
    executor_type: Literal["none", "http", "builtin_function", "python", "javascript"] = Field(
        default="none", alias="executorType"
    )
    executor_config: dict[str, Any] = Field(default_factory=dict, alias="executorConfig")


class BotCommandOrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[str] = Field(min_length=1, max_length=500)


class BotCheckinConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_points: int = Field(alias="minPoints", ge=1, le=1_000_000)
    max_points: int = Field(alias="maxPoints", ge=1, le=1_000_000)


class MessageProbeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=4096)
    timeout_seconds: int = Field(default=30, alias="timeoutSeconds", ge=1, le=120)


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release_note: str | None = Field(default=None, alias="releaseNote", max_length=500)


def _iso(value: datetime | str | None) -> str | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return utc_isoformat(parsed)
    return utc_isoformat(value)


def _redact_step_buttons(step: dict[str, Any]) -> None:
    buttons = step.get("botButtons")
    if not isinstance(buttons, list):
        return
    for row in buttons:
        if not isinstance(row, list):
            continue
        for index, label in enumerate(row):
            if isinstance(label, str):
                row[index] = redact_sensitive(label)


def _task_json(task: Task, database: Database, service: CheckinService) -> dict[str, Any]:
    account = database.get_account_by_id(task.account_id)
    run = service.get_task_run_progress(task.id)
    if run is not None:
        if isinstance(run.get("error"), str):
            run["error"] = redact_sensitive(run["error"])
        for step in run["stepStatuses"]:
            if isinstance(step.get("error"), str):
                step["error"] = redact_sensitive(step["error"])
            if isinstance(step.get("botResponse"), str):
                step["botResponse"] = redact_sensitive(step["botResponse"])
            _redact_step_buttons(step)
        for log in run.get("logs", []):
            if isinstance(log.get("message"), str):
                log["message"] = redact_sensitive(log["message"])
    schedule = schedule_from_task(task).model_dump(mode="json", exclude_none=True)
    versions = database.list_workflow_versions(task.id)
    latest_version = versions[0] if versions else None
    return {
        "id": task.id,
        "name": task.name,
        "account": account.name if account else None,
        "accountId": task.account_id,
        "target": task.target,
        "timezone": task.timezone,
        "schedule": schedule,
        "definition": TaskDefinition.model_validate(task.config).to_api_dict(),
        "enabled": task.enabled,
        "archived": task.archived,
        "running": task.id in service.running or database.has_running_run(task.id),
        "nextRunAt": _iso(task.next_run_at),
        "lastRunAt": _iso(task.last_run_at),
        "lastStatus": task.last_status,
        "run": run,
        "createdAt": _iso(task.created_at),
        "updatedAt": _iso(task.updated_at),
        "latestWorkflowVersion": latest_version.version_number if latest_version else None,
        "workflowVersions": [
            {
                "id": item.id,
                "version": item.version_number,
                "publishedAt": _iso(item.published_at),
                "releaseNote": item.release_note,
            }
            for item in versions
        ],
    }


def _run_json(
    run: TaskRun,
    database: Database,
    service: CheckinService | None = None,
    *,
    include_workflow: bool = True,
    include_progress_logs: bool = True,
) -> dict[str, Any]:
    task = database.get_task_any(run.task_id)
    progress = service.get_task_run_progress(run.task_id) if service and task else None
    if progress is not None and progress.get("id") != run.id:
        progress = None
    if progress is None and run.progress_json:
        try:
            stored_progress = json.loads(run.progress_json)
            if isinstance(stored_progress, dict) and stored_progress.get("id") == run.id:
                progress = stored_progress
        except (TypeError, json.JSONDecodeError):
            progress = None
    if progress is not None:
        progress = copy.deepcopy(
            {
                key: value
                for key, value in progress.items()
                if include_progress_logs or key != "logs"
            }
        )
        if isinstance(progress.get("error"), str):
            progress["error"] = redact_sensitive(progress["error"])
        for step in progress.get("stepStatuses", []):
            if isinstance(step.get("error"), str):
                step["error"] = redact_sensitive(step["error"])
            if isinstance(step.get("botResponse"), str):
                step["botResponse"] = redact_sensitive(step["botResponse"])
            _redact_step_buttons(step)
        if include_progress_logs:
            for log in progress.get("logs", []):
                if isinstance(log.get("message"), str):
                    log["message"] = redact_sensitive(log["message"])
    result = {
        "id": run.id,
        "taskId": run.task_id,
        "taskName": task.name if task else None,
        "timezone": task.timezone if task else "UTC",
        "plannedAt": _iso(run.planned_at),
        "startedAt": _iso(run.started_at),
        "finishedAt": _iso(run.finished_at),
        "status": run.status,
        "attempts": run.attempts,
        "error": redact_sensitive(run.error) if run.error else None,
        "runKind": run.run_kind,
        "workflowVersion": (
            int(run.workflow_version)
            if run.workflow_version and run.workflow_version.isdigit()
            else run.workflow_version
        ),
        "workflowVersionId": run.workflow_version_id,
        "progress": progress,
    }
    if include_workflow:
        workflow: dict[str, Any] | None = None
        if run.run_kind == "test" and run.workflow_json:
            try:
                stored_workflow = json.loads(run.workflow_json)
                if isinstance(stored_workflow, dict):
                    workflow = stored_workflow
            except (TypeError, json.JSONDecodeError):
                workflow = None
        version: WorkflowVersion | None = None
        if run.workflow_version_id:
            version = database.get_workflow_version(run.workflow_version_id)
        if workflow is None and version is not None:
            workflow = version.execution_definition
        workflow_error = None
        if run.run_kind == "published" and workflow is None:
            workflow_error = "发布版本数据不存在"
        result["workflow"] = workflow
        result["workflowError"] = workflow_error
    return result


def _flow_json(flow: Any) -> dict[str, Any]:
    return {
        "flowId": flow.flow_id,
        "accountName": flow.account_name,
        "accountId": flow.account_id,
        "method": flow.method,
        "stage": flow.stage,
        "qrUrl": flow.qr_url,
        "qrExpiresAt": _iso(flow.qr_expires_at),
        "createdAt": _iso(flow.created_at),
        "updatedAt": _iso(flow.updated_at),
    }


def _account_json(account: Any, database: Database) -> dict[str, Any]:
    if hasattr(account, "account_id"):
        summary = database.account_task_summary(account.account_id)
        return {
            "id": account.account_id,
            "name": account.name,
            "phoneMasked": account.phone_masked,
            "active": account.is_active,
            "enabledTaskCount": summary["enabled"],
            "taskCount": summary["total"],
            "createdAt": _iso(account.created_at),
        }
    return {
        "id": account.id,
        "name": account.name,
        "phoneMasked": None,
        "active": account.is_active,
        "enabledTaskCount": 0,
        "taskCount": 0,
        "createdAt": _iso(account.created_at),
    }


def _dashboard_trend(
    database: Database,
    selected_range: Literal["24h", "7d", "30d"],
    now: datetime,
) -> tuple[datetime, list[dict[str, Any]]]:
    now = now.astimezone(timezone.utc)
    if selected_range == "24h":
        bucket_count = 24
        bucket_width = timedelta(hours=1)
        first_bucket = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
        label = lambda value: utc_isoformat(value) or ""
    else:
        bucket_count = 7 if selected_range == "7d" else 30
        bucket_width = timedelta(days=1)
        first_bucket = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=bucket_count - 1
        )
        label = lambda value: value.date().isoformat()

    buckets: list[dict[str, Any]] = [
        {"label": label(first_bucket + index * bucket_width), "success": 0, "failed": 0}
        for index in range(bucket_count)
    ]
    width_seconds = bucket_width.total_seconds()
    for started_at, status in database.dashboard_run_events(first_bucket):
        if status not in {"success", "failed"}:
            continue
        normalized = (
            started_at.replace(tzinfo=timezone.utc)
            if started_at.tzinfo is None
            else started_at.astimezone(timezone.utc)
        )
        index = int((normalized - first_bucket).total_seconds() // width_seconds)
        if 0 <= index < bucket_count:
            buckets[index][status] += 1
    return first_bucket, buckets


def _upcoming_task_json(task: Task, database: Database) -> dict[str, Any]:
    account = database.get_account_by_id(task.account_id)
    return {
        "id": task.id,
        "name": task.name,
        "account": account.name if account else None,
        "accountId": task.account_id,
        "target": task.target,
        "timezone": task.timezone,
        "nextRunAt": _iso(task.next_run_at),
    }


def _validation_details(exc: Exception) -> list[dict[str, Any]]:
    if hasattr(exc, "errors"):
        items = []
        for error in exc.errors():
            items.append(
                {
                    "path": ".".join(str(part) for part in error.get("loc", ())),
                    "message": error.get("msg", "输入无效"),
                    "type": error.get("type", "validation_error"),
                }
            )
        return items
    return [{"path": "", "message": "输入无效", "type": "validation_error"}]


def _parse_task_yaml(value: str) -> TaskDefinition:
    try:
        raw = yaml.safe_load(value)
        if not isinstance(raw, dict):
            raise ValueError("YAML 顶层必须是对象")
        return TaskDefinition.model_validate(raw)
    except Exception as exc:
        raise APIError(
            "VALIDATION_FAILED", "YAML 任务配置无效", 422, details=_validation_details(exc)
        ) from exc


def _read_log_entries(settings: Settings) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    secrets = [settings.api_hash or "", settings.database_url_override or ""]
    if settings.admin_key:
        secrets.append(settings.admin_key.get_secret_value())
    if settings.notification_bot_token:
        secrets.append(settings.notification_bot_token.get_secret_value())
    if settings.admin_bot_token:
        secrets.append(settings.admin_bot_token.get_secret_value())
    if settings.bot_webhook_secret:
        secrets.append(settings.bot_webhook_secret.get_secret_value())
    # Oldest backup first, current log last.
    paths = list(reversed(allowed_log_files(settings.log_path, settings.log_backup_count)))
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            safe_line = redact_sensitive(line, secrets)
            matched = _LOG_PATTERN.match(safe_line)
            if matched:
                item = matched.groupdict()
                item["source"] = path.name
                entries.append(item)
            elif entries:
                entries[-1]["message"] += "\n" + safe_line
            else:
                entries.append(
                    {
                        "timestamp": None,
                        "level": None,
                        "logger": None,
                        "message": safe_line,
                        "source": path.name,
                    }
                )
    return entries


def _filter_logs(
    entries: list[dict[str, Any]],
    level: str | None,
    query: str | None,
    started_from: datetime | None,
    started_to: datetime | None,
) -> list[dict[str, Any]]:
    if started_from:
        started_from = (
            started_from.replace(tzinfo=timezone.utc)
            if started_from.tzinfo is None
            else started_from.astimezone(timezone.utc)
        )
    if started_to:
        started_to = (
            started_to.replace(tzinfo=timezone.utc)
            if started_to.tzinfo is None
            else started_to.astimezone(timezone.utc)
        )
    wanted = level.upper() if level else None
    needle = query.casefold() if query else None
    result = []
    for item in entries:
        if wanted and item.get("level") != wanted:
            continue
        if needle and needle not in json.dumps(item, ensure_ascii=False).casefold():
            continue
        timestamp = item.get("timestamp")
        if timestamp and (started_from or started_to):
            try:
                parsed = datetime.fromisoformat(timestamp.replace(" ", "T", 1))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if started_from and parsed < started_from:
                    continue
                if started_to and parsed > started_to:
                    continue
            except ValueError:
                pass
        result.append(item)
    return result


def create_admin_app(settings: Settings, database: Database, service: CheckinService) -> FastAPI:
    admin_key, admin_origin = settings.require_admin_config()
    keys = TransportKeyManager(rotation_hours=settings.transport_key_rotation_hours)
    # Persist only hashed session tokens and timestamps so browser sessions
    # remain valid across service restarts without storing administrator keys.
    sessions = SessionManager(
        admin_key,
        session_days=settings.admin_session_days,
        session_store=database,
    )
    limiter = FailureRateLimiter(max_failures=5, window_seconds=600)
    accounts = LoginFlowManager(settings, database, client_pool=service.pool)
    admin_bot = TelegramManagementBot(settings, database, service)
    trusted_proxies = [item.strip() for item in settings.trusted_proxies.split(",") if item.strip()]
    try:
        for item in trusted_proxies:
            ipaddress.ip_network(item, strict=False)
    except ValueError as exc:
        raise RuntimeError("TG_BOT_TRUSTED_PROXIES 包含无效 CIDR") from exc
    started_at = time.monotonic()
    shutdown_event = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        rotation_task: asyncio.Task[None] | None = None
        started = False
        previous_signal_handlers = _install_shutdown_signal_handlers(shutdown_event)
        try:
            await service.start()
            started = True
            await service.notifications.service_started()
            await admin_bot.start()
            rotation_task = asyncio.create_task(keys.rotation_loop(), name="admin-key-rotation")
            logger.info("后台管理 API 已启动")
            yield
        finally:
            shutdown_event.set()
            try:
                if rotation_task:
                    rotation_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await rotation_task
                await admin_bot.close()
                await accounts.close()
                if started:
                    await service.notifications.service_stopped("管理 API 服务停止")
                await service.close()
            finally:
                _restore_signal_handlers(previous_signal_handlers)

    app = FastAPI(
        title="tg-bot 后台管理 API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.transport_keys = keys
    app.state.sessions = sessions
    app.state.login_flows = accounts
    app.state.database = database
    app.state.checkin_service = service
    app.state.shutdown_event = shutdown_event
    app.state.admin_bot = admin_bot

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

    @app.get("/api/auth/key")
    async def auth_key(
        purpose: Literal["admin", "phone", "code", "password"] = "admin",
    ) -> JSONResponse:
        return JSONResponse(keys.issue_challenge(purpose), headers={"Cache-Control": "no-store"})

    @app.post(ADMIN_BOT_WEBHOOK_PATH, include_in_schema=False, status_code=204)
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

    @app.post("/api/auth/verify")
    async def auth_verify(request: Request, body: AdminVerifyBody) -> JSONResponse:
        peer = request.client.host if request.client else "unknown"
        client_ip = resolve_client_ip(peer, request.headers.get("X-Forwarded-For"), trusted_proxies)
        limiter.check(client_ip)
        try:
            keys.verify_admin_payload(body.key_id, body.ciphertext, admin_key, purpose="admin")
        except SecurityError:
            limiter.record_failure(client_ip)
            raise SecurityError("AUTH_FAILED", "管理员身份验证失败", status_code=401)
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

    @app.get("/api/auth/session")
    async def auth_session(request: Request) -> dict[str, Any]:
        credentials = request.state.session
        return {
            "authenticated": True,
            "csrfToken": credentials.csrf_token,
            "sessionExpiresAt": _iso(credentials.expires_at),
        }

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request, _: EmptyBody) -> Response:
        sessions.revoke(request.state.session.token)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/api", secure=True, samesite="strict")
        return response

    @app.get("/api/dashboard")
    async def dashboard(range: Literal["24h", "7d", "30d"] = "24h") -> dict[str, Any]:
        now = utc_now()
        since, status_breakdown = _dashboard_trend(database, range, now)
        raw_stats = database.dashboard_stats(since)
        success_runs = int(raw_stats["runs_success"])
        failed_runs = int(raw_stats["runs_failed"])
        completed_runs = success_runs + failed_runs
        total_tasks = int(raw_stats["tasks_total"])
        enabled_tasks = int(raw_stats["tasks_enabled"])
        running_tasks = len(service.running)
        success_rate = round(success_runs * 100 / completed_runs, 1) if completed_runs else 0.0
        stats = {
            "totalTasks": total_tasks,
            "enabledTasks": enabled_tasks,
            "runningTasks": running_tasks,
            "failedRuns": failed_runs,
            "successRate": success_rate,
            # Compatibility aliases retained for existing API consumers.
            "tasksTotal": raw_stats["tasks_total"],
            "tasksEnabled": raw_stats["tasks_enabled"],
            "tasksArchived": raw_stats["tasks_archived"],
            "runsTotal": raw_stats["runs_total"],
            "runsSuccess": raw_stats["runs_success"],
            "runsFailed": raw_stats["runs_failed"],
            "runsCanceled": raw_stats["runs_canceled"],
            "runsSkipped": raw_stats["runs_skipped"],
            "runsRunning": raw_stats["runs_running"],
        }
        account_items = database.list_accounts()
        active_accounts = sum(account.is_active for account in account_items)
        inactive_accounts = len(account_items) - active_accounts
        scheduler_health = "healthy" if service.scheduler.running else "unhealthy"
        telegram_health = (
            "healthy"
            if account_items and inactive_accounts == 0
            else "degraded"
            if active_accounts
            else "unhealthy"
        )
        database_health = "healthy"
        service_health = (
            "healthy"
            if scheduler_health == "healthy" and database_health == "healthy"
            else "degraded"
        )
        recent, _ = database.list_runs(page=1, page_size=10, started_from=since)
        upcoming = database.upcoming_tasks(limit=10)
        return {
            "range": range,
            "health": {
                "service": service_health,
                "database": database_health,
                "scheduler": scheduler_health,
                "telegram": telegram_health,
                "status": service_health,
                "schedulerRunning": service.scheduler.running,
                "uptimeSeconds": int(time.monotonic() - started_at),
                "runningTasks": running_tasks,
                "checkedAt": _iso(now),
            },
            "stats": stats,
            "statusBreakdown": status_breakdown,
            "upcomingTasks": [_upcoming_task_json(item, database) for item in upcoming],
            "accountStatus": [
                {"status": "active", "label": "正常", "count": active_accounts},
                {"status": "inactive", "label": "停用", "count": inactive_accounts},
            ],
            "accountSummary": {
                "total": len(account_items),
                "active": active_accounts,
                "inactive": inactive_accounts,
            },
            "recentRuns": [_run_json(item, database, service) for item in recent],
        }

    @app.get("/api/tasks")
    async def list_tasks(
        page: int = Query(1, ge=1),
        page_size: int = Query(25, alias="pageSize", ge=1, le=100),
        include_archived: bool = Query(False, alias="includeArchived"),
        enabled: bool | None = None,
        search: str | None = Query(None, max_length=200),
    ) -> dict[str, Any]:
        items, total = database.list_tasks_page(
            page=page,
            page_size=page_size,
            include_archived=include_archived,
            enabled=enabled,
            search=search,
        )
        return {
            "items": [_task_json(item, database, service) for item in items],
            "page": page,
            "pageSize": page_size,
            "total": total,
        }

    @app.post("/api/tasks", status_code=201)
    async def create_task(body: TaskBody) -> dict[str, Any]:
        if database.get_account(body.definition.account) is None:
            raise APIError("VALIDATION_FAILED", "Telegram 账号不存在", 422)
        try:
            task = service.create_task(body.definition)
        except TaskStateError as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc
        return _task_json(task, database, service)

    @app.post("/api/tasks/validate")
    async def validate_task(body: TaskBody) -> dict[str, Any]:
        if database.get_account(body.definition.account) is None:
            raise APIError(
                "VALIDATION_FAILED",
                "任务配置无效",
                422,
                details=[{"path": "definition.account", "message": "Telegram 账号不存在"}],
            )
        return {
            "valid": True,
            "definition": body.definition.to_api_dict(),
        }

    @app.post("/api/tasks/preview")
    async def preview_task(body: TaskBody) -> dict[str, Any]:
        schedule = body.definition.schedule
        runs = next_runs(schedule, now=utc_now(), count=5)
        return {
            "items": [_iso(item) for item in runs],
            "timezone": schedule.timezone,
        }

    @app.post("/api/tasks/{task_id}/preview")
    async def preview_existing_task(task_id: str, body: TaskBody) -> dict[str, Any]:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        schedule = body.definition.schedule
        start_after = task.next_run_at - timedelta(microseconds=1) if task.next_run_at else None
        runs = next_runs(schedule, now=utc_now(), count=5, start_after=start_after)
        return {
            "items": [_iso(item) for item in runs],
            "timezone": schedule.timezone,
        }

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        return _task_json(task, database, service)

    @app.get("/api/tasks/{task_id}/versions")
    async def list_workflow_versions(task_id: str) -> dict[str, Any]:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        versions = database.list_workflow_versions(task.id)
        return {
            "items": [
                {
                    "id": item.id,
                    "version": item.version_number,
                    "publishedAt": _iso(item.published_at),
                    "releaseNote": item.release_note,
                    "workflow": item.execution_definition,
                }
                for item in versions
            ],
            "latestVersion": versions[0].version_number if versions else None,
        }

    @app.get("/api/tasks/{task_id}/events")
    async def stream_task_events(task_id: str, request: Request) -> StreamingResponse:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)

        async def events() -> AsyncIterator[str]:
            queue = service.subscribe_task(task.id)
            try:
                event_id = service.next_task_event_id()
                current = database.get_task_any(task.id)
                if current is None:
                    return
                payload = json.dumps(
                    _task_json(current, database, service),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"id: {event_id}\nevent: task.updated\ndata: {payload}\n\n"

                while not shutdown_event.is_set() and not await request.is_disconnected():
                    queue_get = asyncio.create_task(queue.get())
                    shutdown_wait = asyncio.create_task(shutdown_event.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {queue_get, shutdown_wait},
                            timeout=TASK_EVENT_KEEPALIVE_SECONDS,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        for pending_task in (queue_get, shutdown_wait):
                            if not pending_task.done():
                                pending_task.cancel()
                        await asyncio.gather(queue_get, shutdown_wait, return_exceptions=True)
                    if shutdown_wait in done:
                        return
                    if queue_get not in done:
                        yield ": keepalive\n\n"
                        continue
                    event_id = queue_get.result()
                    current = database.get_task_any(task.id)
                    if current is None:
                        return
                    payload = json.dumps(
                        _task_json(current, database, service),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"id: {event_id}\nevent: task.updated\ndata: {payload}\n\n"
            finally:
                service.unsubscribe_task(task.id, queue)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.patch("/api/tasks/{task_id}")
    async def edit_task(task_id: str, body: TaskBody) -> dict[str, Any]:
        if database.get_account(body.definition.account) is None:
            raise APIError("VALIDATION_FAILED", "Telegram 账号不存在", 422)
        try:
            task = service.edit_task(task_id, body.definition)
        except TaskNotFound as exc:
            raise APIError("NOT_FOUND", "任务不存在", 404) from exc
        except TaskStateError as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc
        return _task_json(task, database, service)

    @app.post("/api/tasks/{task_id}/publish")
    async def publish_task(task_id: str, body: PublishBody) -> dict[str, Any]:
        try:
            service.publish_task(task_id, body.release_note)
        except TaskNotFound as exc:
            raise APIError("NOT_FOUND", "任务不存在", 404) from exc
        except TaskStateError as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc
        task = database.get_task_any(task_id)
        if task is None:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        return _task_json(task, database, service)

    async def task_action(task_id: str, action: str) -> dict[str, Any]:
        try:
            task = getattr(service, f"{action}_task")(task_id)
        except TaskNotFound as exc:
            raise APIError("NOT_FOUND", "任务不存在", 404) from exc
        except TaskStateError as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc
        return _task_json(task, database, service)

    @app.post("/api/tasks/{task_id}/enable")
    async def enable_task(task_id: str, _: EmptyBody):
        return await task_action(task_id, "enable")

    @app.post("/api/tasks/{task_id}/disable")
    async def disable_task(task_id: str, _: EmptyBody):
        return await task_action(task_id, "disable")

    @app.post("/api/tasks/{task_id}/skip-next")
    async def skip_next_task(task_id: str, _: EmptyBody):
        return await task_action(task_id, "skip_next")

    @app.post("/api/tasks/{task_id}/archive")
    async def archive_task(task_id: str, _: EmptyBody):
        return await task_action(task_id, "archive")

    @app.post("/api/tasks/{task_id}/restore")
    async def restore_task(task_id: str, _: EmptyBody):
        return await task_action(task_id, "restore")

    @app.post("/api/tasks/{task_id}/run", status_code=202)
    async def run_task(task_id: str, _: EmptyBody) -> dict[str, Any]:
        try:
            run_id = service.start_manual_run(task_id)
        except TaskNotFound as exc:
            raise APIError("NOT_FOUND", "任务不存在", 404) from exc
        except ManualRunConflict as exc:
            raise APIError("TASK_BUSY", "同一账号和目标已有任务在执行", 409) from exc
        except TaskStateError as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc
        task = database.get_task_any(task_id)
        if task is None:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        payload = _task_json(task, database, service)
        payload["runId"] = run_id
        return payload

    @app.post("/api/tasks/{task_id}/test", status_code=202)
    async def test_task(task_id: str, body: TaskBody) -> dict[str, Any]:
        if database.get_account(body.definition.account) is None:
            raise APIError("VALIDATION_FAILED", "Telegram 账号不存在", 422)
        try:
            run_id = service.start_test_run(task_id, body.definition)
        except TaskNotFound as exc:
            raise APIError("NOT_FOUND", "任务不存在", 404) from exc
        except ManualRunConflict as exc:
            raise APIError("TASK_BUSY", "同一账号和目标已有任务在执行", 409) from exc
        except TaskStateError as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc
        task = database.get_task_any(task_id)
        if task is None:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        payload = _task_json(task, database, service)
        payload["runId"] = run_id
        return payload

    @app.post("/api/tasks/{task_id}/cancel", status_code=202)
    async def cancel_task(task_id: str, _: EmptyBody) -> dict[str, Any]:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        if not await service.cancel_task(task.id):
            raise APIError("RUN_NOT_ACTIVE", "任务当前没有运行实例", 409)
        return {"accepted": True, "taskId": task.id}

    @app.get("/api/tasks/{task_id}/export")
    async def export_task(task_id: str) -> Response:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        content = TaskDefinition.model_validate(task.config).to_yaml()
        return Response(
            content,
            media_type="application/yaml; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="task-{task.id}.yaml"'},
        )

    @app.post("/api/tasks/import/preflight")
    async def import_preflight(body: ImportBody) -> dict[str, Any]:
        try:
            definition = _parse_task_yaml(body.yaml)
        except APIError as exc:
            return {
                "valid": False,
                "conflicts": [],
                "task": None,
                "errors": exc.details or [],
            }
        existing = database.get_task_any(definition.name)
        errors = []
        if database.get_account(definition.account) is None:
            errors.append({"path": "account", "message": "Telegram 账号不存在"})
        return {
            "valid": not errors,
            "conflicts": [definition.name] if existing else [],
            "task": definition.to_api_dict(),
            "errors": errors,
        }

    @app.post("/api/tasks/import")
    async def import_task(body: ImportBody) -> dict[str, Any]:
        definition = _parse_task_yaml(body.yaml)
        if database.get_account(definition.account) is None:
            raise APIError("VALIDATION_FAILED", "Telegram 账号不存在", 422)
        existing = database.get_task_any(definition.name)
        if existing and definition.name not in body.overwrite_names:
            raise APIError(
                "CONFLICT",
                "同名任务已存在，必须明确选择覆盖",
                409,
                details={"conflicts": [definition.name]},
            )
        try:
            if existing and existing.archived:
                service.restore_task(existing.id)
            task = (
                service.edit_task(existing.id, definition)
                if existing
                else service.create_task(definition)
            )
        except TaskStateError as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc
        return {"imported": [_task_json(task, database, service)], "overwritten": bool(existing)}

    @app.get("/api/runs")
    async def list_runs(
        page: int = Query(1, ge=1),
        page_size: int = Query(25, alias="pageSize", ge=1, le=100),
        task_id: str | None = Query(None, alias="taskId"),
        status: str | None = None,
        started_from: datetime | None = Query(None, alias="from"),
        started_to: datetime | None = Query(None, alias="to"),
    ) -> dict[str, Any]:
        items, total = database.list_runs(
            page=page,
            page_size=page_size,
            task_id=task_id,
            status=status,
            started_from=started_from,
            started_to=started_to,
        )
        return {
            "items": [
                _run_json(
                    item,
                    database,
                    service,
                    include_workflow=False,
                    include_progress_logs=False,
                )
                for item in items
            ],
            "page": page,
            "pageSize": page_size,
            "total": total,
        }

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = database.get_run(run_id)
        if not run:
            raise APIError("NOT_FOUND", "执行记录不存在", 404)
        return _run_json(run, database, service)

    @app.get("/api/accounts")
    async def list_accounts() -> dict[str, Any]:
        items = accounts.list_accounts()
        return {"items": [_account_json(item, database) for item in items], "total": len(items)}

    @app.get("/api/accounts/{account_id}/chats")
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

    @app.post("/api/accounts/{account_id}/chats/pull")
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

    @app.post("/api/accounts/{account_id}/messages/probe")
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

    @app.get("/api/avatar/{chat_id}/{avatar_photo_id}")
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

    @app.post("/api/accounts/login-flows", status_code=201)
    async def start_login(body: LoginStartBody) -> dict[str, Any]:
        return _flow_json(await accounts.start(body.account_name, body.method))

    @app.get("/api/accounts/login-flows/{flow_id}")
    async def get_login(flow_id: str) -> dict[str, Any]:
        return _flow_json(await accounts.get_flow(flow_id))

    def decrypt_sensitive(body: EncryptedBody, purpose: str) -> str:
        payload = keys.decrypt_payload(body.key_id, body.ciphertext, purpose)
        value = payload.get("value")
        if not isinstance(value, str) or not value:
            raise APIError("VALIDATION_FAILED", "敏感输入无效", 422)
        return value

    @app.post("/api/accounts/login-flows/{flow_id}/phone")
    async def submit_phone(flow_id: str, body: EncryptedBody) -> dict[str, Any]:
        value = decrypt_sensitive(body, "phone")
        try:
            return _flow_json(await accounts.submit_phone(flow_id, value))
        finally:
            value = ""

    @app.post("/api/accounts/login-flows/{flow_id}/code")
    async def submit_code(flow_id: str, body: EncryptedBody) -> dict[str, Any]:
        value = decrypt_sensitive(body, "code")
        try:
            return _flow_json(await accounts.submit_code(flow_id, value))
        finally:
            value = ""

    @app.post("/api/accounts/login-flows/{flow_id}/password")
    async def submit_password(flow_id: str, body: EncryptedBody) -> dict[str, Any]:
        value = decrypt_sensitive(body, "password")
        try:
            return _flow_json(await accounts.submit_password(flow_id, value))
        finally:
            value = ""

    @app.delete("/api/accounts/login-flows/{flow_id}", status_code=204)
    async def cancel_login(flow_id: str) -> Response:
        await accounts.cancel(flow_id)
        return Response(status_code=204)

    @app.get("/api/accounts/{account_id}/logout-impact")
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

    @app.post("/api/accounts/{account_id}/logout")
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

    @app.post("/api/settings/transport-key/rotate")
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

    @app.get("/api/logs")
    async def list_logs(
        page: int = Query(1, ge=1),
        page_size: int = Query(25, alias="pageSize", ge=1, le=100),
        level: str | None = None,
        query: str | None = Query(None, max_length=200),
        started_from: datetime | None = Query(None, alias="from"),
        started_to: datetime | None = Query(None, alias="to"),
    ) -> dict[str, Any]:
        entries = _filter_logs(_read_log_entries(settings), level, query, started_from, started_to)
        entries.reverse()
        start = (page - 1) * page_size
        return {
            "items": entries[start : start + page_size],
            "page": page,
            "pageSize": page_size,
            "total": len(entries),
        }

    @app.get("/api/logs/stream")
    async def stream_logs(
        request: Request,
        level: str | None = None,
        query: str | None = Query(None, max_length=200),
    ) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            seen = len(_filter_logs(_read_log_entries(settings), level, query, None, None))
            last_keepalive = time.monotonic()
            while not shutdown_event.is_set() and not await request.is_disconnected():
                entries = _filter_logs(_read_log_entries(settings), level, query, None, None)
                if len(entries) < seen:
                    seen = 0
                for item in entries[seen:]:
                    yield f"event: log\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                seen = len(entries)
                if time.monotonic() - last_keepalive >= 15:
                    yield ": keepalive\n\n"
                    last_keepalive = time.monotonic()
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=LOG_EVENT_POLL_SECONDS)
                except TimeoutError:
                    pass

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/logs/download")
    async def download_logs() -> Response:
        files = allowed_log_files(settings.log_path, settings.log_backup_count)
        secrets = [settings.api_hash or "", settings.database_url_override or ""]
        if settings.admin_key:
            secrets.append(settings.admin_key.get_secret_value())
        if settings.notification_bot_token:
            secrets.append(settings.notification_bot_token.get_secret_value())
        if settings.admin_bot_token:
            secrets.append(settings.admin_bot_token.get_secret_value())
        if settings.bot_webhook_secret:
            secrets.append(settings.bot_webhook_secret.get_secret_value())
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                try:
                    archive.writestr(
                        path.name,
                        redact_sensitive(path.read_text("utf-8", errors="replace"), secrets),
                    )
                except OSError:
                    continue
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="tg-bot-logs.zip"'},
        )

    @app.post("/api/bot/bindings", status_code=201)
    async def create_bot_binding(_: EmptyBody) -> dict[str, Any]:
        code, item = admin_bot.management.create_binding_code()
        admin_bot.management.audit(None, None, "binding_code_create", "success", details=json.dumps({"role": "user", "quantity": 1, "ttlDays": 0}, ensure_ascii=False))
        return {
            "id": item.id,
            "code": code,
            "createdAt": _iso(item.created_at),
            "expiresAt": None if item.expires_at is not None and item.expires_at.year >= 9999 else _iso(item.expires_at),
            "role": item.role or "user",
        }

    @app.post("/api/bot/binding-codes/batch", status_code=201)
    async def create_bot_binding_batch(request: Request, body: BotBindingBatchBody) -> dict[str, Any]:
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
        admin_bot.management.audit(None, None, "binding_code_batch_create", "success", details=json.dumps({"role": body.role, "batchId": batch_id, "quantity": body.quantity, "ttlDays": body.ttl_days}, ensure_ascii=False))
        return {
            "batchId": batch_id,
            "codes": [
                {"id": item.id, "code": code, "hint": item.code_hint, "role": item.role or body.role,
                 "createdAt": _iso(item.created_at), "expiresAt": None if item.expires_at is not None and item.expires_at.year >= 9999 else _iso(item.expires_at)}
                for code, item in generated
            ],
        }

    @app.post("/api/bot/binding-codes/admin", status_code=201)
    async def create_admin_bot_binding(request: Request, body: BotAdminBindingBody) -> dict[str, Any]:
        try:
            batch_id, generated = admin_bot.management.create_binding_codes(1, body.ttl_days, role="admin", idempotency_key=request.headers.get("Idempotency-Key"))
        except (BotBindingError, ValueError) as exc:
            code = "IDEMPOTENCY_CONFLICT" if str(exc) == "IDEMPOTENCY_CONFLICT" else "VALIDATION_FAILED"
            raise APIError(code, "管理员绑定码请求无效", 409 if code == "IDEMPOTENCY_CONFLICT" else 400) from exc
        code_value, item = generated[0]
        admin_bot.management.audit(None, None, "binding_code_create", "success", details=json.dumps({"role": "admin", "quantity": 1, "ttlDays": body.ttl_days}, ensure_ascii=False))
        return {"batchId": batch_id, "id": item.id, "code": code_value, "role": "admin", "createdAt": _iso(item.created_at), "expiresAt": None if item.expires_at is not None and item.expires_at.year >= 9999 else _iso(item.expires_at)}

    @app.get("/api/bot/binding-codes")
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
                    "expiresAt": None if item.expires_at is not None and item.expires_at.year >= 9999 else _iso(item.expires_at),
                    "usedAt": _iso(item.used_at),
                    "revokedAt": _iso(item.revoked_at),
                }
                for item in codes
            ],
            "codesPagination": {"page": page, "pageSize": page_size, "total": total},
        }

    @app.get("/api/bot/bindings")
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

    @app.delete("/api/bot/binding-codes/{code_id}", status_code=204)
    async def revoke_bot_binding_code(code_id: str) -> Response:
        if not admin_bot.management.revoke_code(code_id):
            raise APIError("NOT_FOUND", "绑定码不存在或已失效", 404)
        return Response(status_code=204)

    @app.delete("/api/bot/bindings/{binding_id}", status_code=204)
    async def revoke_bot_binding(binding_id: str) -> Response:
        if not admin_bot.management.revoke_binding(binding_id):
            raise APIError("NOT_FOUND", "绑定关系不存在或已解除", 404)
        return Response(status_code=204)

    @app.get("/api/bot/commands")
    async def list_bot_commands() -> dict[str, Any]:
        return {"commands": admin_bot.command_configs()}

    @app.get("/api/bot/checkin-config")
    async def get_bot_checkin_config() -> dict[str, int]:
        return admin_bot.management.checkin_config()

    @app.patch("/api/bot/checkin-config")
    @app.put("/api/bot/checkin-config")
    async def update_bot_checkin_config(body: BotCheckinConfigBody) -> dict[str, int]:
        try:
            return admin_bot.management.update_checkin_config(body.min_points, body.max_points)
        except BotBindingError as exc:
            raise APIError("VALIDATION_FAILED", str(exc), 422) from exc

    @app.post("/api/bot/commands", status_code=201)
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

    @app.post("/api/bot/commands/pull")
    async def pull_bot_commands(_: EmptyBody) -> dict[str, Any]:
        if admin_bot.client is None:
            raise APIError("BOT_NOT_CONFIGURED", "Telegram 管理 Bot 未配置", 503)
        try:
            commands = await admin_bot.pull_remote_commands()
        except Exception as exc:
            logger.warning("手动拉取管理 Bot 指令失败 type=%s", type(exc).__name__)
            raise APIError("COMMAND_PULL_FAILED", "从 Telegram 拉取指令失败", 502) from exc
        return {"commands": commands}

    @app.put("/api/bot/commands/order")
    async def reorder_bot_commands(body: BotCommandOrderBody) -> dict[str, Any]:
        try:
            return {"commands": admin_bot.management.reorder_command_configs(body.commands)}
        except (BotCommandValidationError, ValueError) as exc:
            raise APIError("VALIDATION_FAILED", str(exc), 422) from exc

    @app.post("/api/bot/commands/sync")
    async def sync_bot_commands(_: EmptyBody) -> dict[str, Any]:
        if admin_bot.client is None:
            raise APIError("BOT_NOT_CONFIGURED", "Telegram 管理 Bot 未配置", 503)
        try:
            await admin_bot.refresh_commands()
        except Exception as exc:
            logger.warning("手动同步管理 Bot 指令失败 type=%s", type(exc).__name__)
            raise APIError("COMMAND_SYNC_FAILED", "向 Telegram 同步指令失败", 502) from exc
        return {"commands": admin_bot.command_configs()}

    @app.patch("/api/bot/commands/{command}")
    @app.put("/api/bot/commands/{command}")
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

    @app.delete("/api/bot/commands/{command}", status_code=204)
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

    @app.get("/api/settings")
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

    @app.get("/api/openapi.json", include_in_schema=False)
    async def protected_openapi() -> JSONResponse:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        return JSONResponse(schema)

    @app.get("/api/docs", include_in_schema=False)
    async def protected_docs() -> Response:
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json", title=f"{app.title} - Swagger UI"
        )

    return app
