from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from tg_botx.features.accounts.models import (
    AdminAccountError,
)
from tg_botx.infrastructure.persistence.db import Account

logger = logging.getLogger(__name__)


class AvatarCache:
    def __init__(self, settings, database, access):
        self.settings = settings
        self.database = database
        self.access = access
        self._avatar_prefetch_tasks: set[asyncio.Task[None]] = set()
        self._avatar_download_locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        tasks = tuple(self._avatar_prefetch_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def download_chat_avatar(self, account_id_or_name: str, chat_id: str) -> Path | None:
        """Return a locally cached avatar without contacting Telegram.

        Avatar files are populated asynchronously while chats are pulled.  A
        request for an avatar must remain a cheap, read-only cache lookup: a
        missing file is represented by ``None`` and the API turns that into a
        404 response.
        """

        account = self.access._find_account(account_id_or_name)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        if not account.is_active:
            raise AdminAccountError("ACCOUNT_INACTIVE", "Telegram 账号已停用")
        if not re.fullmatch(r"-?\d+", chat_id):
            raise AdminAccountError("CHAT_ID_INVALID", "聊天 ID 无效")

        cache_dir = self._avatar_cache_dir()
        try:
            get_account_chat = getattr(self.database, "get_account_chat", None)
            chat = get_account_chat(account.id, chat_id) if get_account_chat else None
        except AdminAccountError:
            raise
        except Exception:
            raise AdminAccountError("CHAT_AVATAR_FAILED", "无法读取聊天头像缓存") from None

        photo_id = getattr(chat, "avatar_photo_id", None) if chat is not None else None
        if photo_id is not None:
            cache_path = cache_dir / f"{chat_id}-{photo_id}.jpg"
            if self._valid_avatar_file(cache_path):
                return cache_path
            # A known photo version with no matching file is a cache miss.
            # Do not serve an older version under the same chat id.
            return None

        # Keep avatars cached by older versions (or rows created before the
        # photo id column existed) usable.  A row explicitly marked as having
        # no avatar must not resurrect a stale file.  This still performs no
        # network or database mutation and simply returns None when no file is
        # present.
        if chat is not None and not getattr(chat, "has_avatar", False):
            return None
        return self._find_legacy_avatar(cache_dir, chat_id)

    def _schedule_avatar_prefetch(
        self,
        account_id: str,
        client: Any,
        jobs: list[tuple[str, Any, int]],
        *,
        account: Account | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._prefetch_chat_avatars(account_id, client, jobs, account=account)
        )
        self._avatar_prefetch_tasks.add(task)

        def on_done(completed: asyncio.Task[None]) -> None:
            self._avatar_prefetch_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception:
                logger.exception("后台预下载聊天头像失败 account_id=%s", account_id)

        task.add_done_callback(on_done)

    async def _prefetch_chat_avatars(
        self,
        account_id: str,
        client: Any,
        jobs: list[tuple[str, Any, int]],
        *,
        account: Account | None = None,
    ) -> None:
        semaphore = asyncio.Semaphore(4)

        async def download(job: tuple[str, Any, int]) -> None:
            chat_id, entity, photo_id = job
            async with semaphore:
                try:
                    await self._download_avatar_file(client, entity, chat_id, photo_id)
                except Exception:
                    logger.warning(
                        "后台下载聊天头像失败 account_id=%s chat_id=%s",
                        account_id,
                        chat_id,
                        exc_info=True,
                    )

        try:
            await asyncio.gather(*(download(job) for job in jobs))
        finally:
            if account is not None:
                await self.access._release_pooled_client(account)

    async def _download_avatar_file(
        self,
        client: Any,
        entity: Any,
        chat_id: str,
        photo_id: int,
    ) -> Path | None:
        cache_dir = self._avatar_cache_dir()
        cache_path = cache_dir / f"{chat_id}-{photo_id}.jpg"
        lock = self._avatar_download_locks.setdefault(str(cache_path), asyncio.Lock())
        async with lock:
            # A pull prefetch and a browser request can arrive at the same
            # time. Re-check after acquiring the lock to avoid duplicate
            # Telegram downloads for the same avatar.
            if self._valid_avatar_file(cache_path):
                return cache_path

            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary_path = cache_dir / f".{cache_path.name}.{uuid.uuid4().hex}.tmp"
            try:
                downloaded = await client.download_profile_photo(entity, file=str(temporary_path))
                if downloaded is None or not temporary_path.is_file():
                    return None
                if temporary_path.stat().st_size <= 0:
                    return None
                temporary_path.replace(cache_path)
            finally:
                temporary_path.unlink(missing_ok=True)

            # Keep only the current photo for this chat.  Old versions are
            # never needed after the photo id has changed.
            for stale_path in cache_dir.glob(f"{chat_id}-*.jpg"):
                if stale_path != cache_path:
                    stale_path.unlink(missing_ok=True)
        return cache_path

    def _avatar_cache_dir(self) -> Path:
        return self.settings.data_dir / "cache" / "avatars"

    @staticmethod
    def _valid_avatar_file(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _find_legacy_avatar(self, cache_dir: Path, chat_id: str) -> Path | None:
        try:
            candidates = [
                path for path in cache_dir.glob(f"{chat_id}-*.jpg") if self._valid_avatar_file(path)
            ]
        except OSError:
            return None
        if not candidates:
            return None
        # There should normally be one file.  Choosing the newest makes the
        # fallback deterministic if an interrupted previous download left
        # multiple versions behind.
        try:
            return max(candidates, key=lambda path: path.stat().st_mtime_ns)
        except OSError:
            return None
