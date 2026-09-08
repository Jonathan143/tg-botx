from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import time
import zipfile
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse

from tg_botx.config import Settings
from tg_botx.infrastructure.observability.logging import allowed_log_files, redact_sensitive
from tg_botx.interfaces.admin.constants import (
    LOG_EVENT_POLL_SECONDS,
)
from tg_botx.interfaces.admin.log_reader import _filter_logs, _read_log_entries

logger = logging.getLogger(__name__)


def build_router(settings: Settings, shutdown_event: asyncio.Event) -> APIRouter:
    router = APIRouter()

    @router.get("/api/logs")
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

    @router.get("/api/logs/stream")
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
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shutdown_event.wait(), timeout=LOG_EVENT_POLL_SECONDS)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.get("/api/logs/download")
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

    return router
