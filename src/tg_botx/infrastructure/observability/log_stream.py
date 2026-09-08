"""共享日志尾读器：按 inode 和偏移增量读取，多个 SSE 订阅者复用一个读取任务。"""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from tg_botx.config import Settings
from tg_botx.infrastructure.observability.logging import allowed_log_files, redact_sensitive

LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T][^ ]+)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>\S+)\s*(?P<message>.*)$"
)


def log_secrets(settings: Settings) -> list[str]:
    values = [settings.api_hash or "", settings.database_url_override or ""]
    for name in ("admin_key", "notification_bot_token", "admin_bot_token", "bot_webhook_secret"):
        secret = getattr(settings, name, None)
        if secret:
            values.append(secret.get_secret_value())
    return values


@dataclass(slots=True)
class FileCursor:
    offset: int
    pending: bytes = b""
    discarding: bool = False


class IncrementalLogReader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.secrets = log_secrets(settings)
        self.cursors: dict[tuple[int, int], FileCursor] = {}

    def prime(self) -> None:
        for path in allowed_log_files(self.settings.log_path, self.settings.log_backup_count):
            with suppress(OSError):
                stat = path.stat()
                self.cursors[(stat.st_dev, stat.st_ino)] = FileCursor(stat.st_size)

    def read(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        active: set[tuple[int, int]] = set()
        paths = reversed(allowed_log_files(self.settings.log_path, self.settings.log_backup_count))
        for path in paths:
            try:
                with path.open("rb") as file:
                    import os

                    stat = os.fstat(file.fileno())
                    identity = (stat.st_dev, stat.st_ino)
                    if identity in active:
                        continue
                    active.add(identity)
                    cursor = self.cursors.setdefault(identity, FileCursor(0))
                    if stat.st_size < cursor.offset:
                        cursor.offset, cursor.pending, cursor.discarding = 0, b"", False
                    file.seek(cursor.offset)
                    chunk = file.read(1024 * 1024)
                    cursor.offset = file.tell()
            except OSError:
                continue
            lines = (cursor.pending + chunk).split(b"\n")
            cursor.pending = lines.pop()
            if cursor.discarding and lines:
                lines.pop(0)
                cursor.discarding = False
            if len(cursor.pending) > 1024 * 1024 or cursor.discarding:
                cursor.pending = b""
                cursor.discarding = True
            file_entries: list[dict[str, Any]] = []
            for line in lines:
                safe = redact_sensitive(
                    line.decode("utf-8", errors="replace").rstrip("\r"), self.secrets
                )
                matched = LOG_PATTERN.match(safe)
                if matched:
                    file_entries.append({**matched.groupdict(), "source": path.name})
                elif file_entries:
                    file_entries[-1]["message"] += "\n" + safe
                else:
                    file_entries.append(
                        {
                            "timestamp": None,
                            "level": None,
                            "logger": None,
                            "message": safe,
                            "source": path.name,
                        }
                    )
            entries.extend(file_entries)
        self.cursors = {key: value for key, value in self.cursors.items() if key in active}
        return entries


class LogStream:
    def __init__(self, settings: Settings):
        self.reader = IncrementalLogReader(settings)
        self.subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stopping = asyncio.Event()

    async def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        async with self._lock:
            if self._task is None:
                await asyncio.to_thread(self.reader.prime)
                self._stopping.clear()
                self._task = asyncio.create_task(self._poll(), name="shared-log-stream")
            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=200)
            self.subscribers.add(queue)
            return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        async with self._lock:
            self.subscribers.discard(queue)
            if not self.subscribers:
                await self._stop()

    async def _poll(self) -> None:
        while not self._stopping.is_set():
            for entry in await asyncio.to_thread(self.reader.read):
                for queue in tuple(self.subscribers):
                    if queue.full():
                        while not queue.empty():
                            queue.get_nowait()
                        queue.put_nowait(None)
                    queue.put_nowait(entry)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=1)

    async def _stop(self) -> None:
        if self._task is not None:
            task, self._task = self._task, None
            self._stopping.set()
            await task

    async def close(self) -> None:
        async with self._lock:
            await self._stop()
            self.subscribers.clear()
