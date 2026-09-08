from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI

logger = logging.getLogger(__name__)


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


def build_lifespan(service, keys, accounts, admin_bot, shutdown_event, log_stream):
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
                await log_stream.close()
                await admin_bot.close()
                await accounts.close()
                if started:
                    await service.notifications.service_stopped("管理 API 服务停止")
                await service.close()
            finally:
                _restore_signal_handlers(previous_signal_handlers)

    return lifespan
