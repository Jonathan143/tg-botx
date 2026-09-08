from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from tg_botx.features.checkin.runtime import (
    CheckinService,
)
from tg_botx.infrastructure.persistence.db import (
    Database,
)
from tg_botx.interfaces.admin.constants import (
    TASK_EVENT_KEEPALIVE_SECONDS,
)
from tg_botx.interfaces.admin.errors import APIError
from tg_botx.interfaces.admin.presenters import _task_json

logger = logging.getLogger(__name__)


def build_router(
    database: Database, service: CheckinService, shutdown_event: asyncio.Event
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/tasks/{task_id}/events")
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

    return router
