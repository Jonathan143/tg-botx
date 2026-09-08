from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from telethon.tl.types import Channel, Chat, User

from tg_botx.features.accounts.models import (
    AdminAccountError,
    ChatPullView,
    ChatView,
)
from tg_botx.infrastructure.persistence.db import utc_now

logger = logging.getLogger(__name__)


class ChatDirectory:
    def __init__(self, database, access, avatars):
        self.database = database
        self.access = access
        self.avatars = avatars

    async def list_chats(
        self,
        account_id_or_name: str,
        *,
        chat_type: Literal["all", "bot", "group", "private"] = "all",
        query: str | None = None,
        limit: int = 200,
    ) -> list[ChatView]:
        account = self.access._find_account(account_id_or_name)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        if not account.is_active:
            raise AdminAccountError("ACCOUNT_INACTIVE", "Telegram 账号已停用")
        if chat_type not in {"all", "bot", "group", "private"}:
            raise AdminAccountError("CHAT_TYPE_INVALID", "聊天类型无效")
        try:
            rows = self.database.list_account_chats(
                account.id,
                chat_type=chat_type,
                query=query,
                limit=limit,
            )
        except Exception:
            raise AdminAccountError("CHAT_LIST_FAILED", "无法加载账号对话") from None
        return [
            ChatView(
                chat_id=row.chat_id,
                chat_type=row.chat_type,
                title=row.title,
                username=row.username,
                has_avatar=row.has_avatar,
                avatar_photo_id=row.avatar_photo_id,
            )
            for row in rows
        ]

    async def pull_chats(
        self,
        account_id_or_name: str,
        *,
        client: Any | None = None,
    ) -> ChatPullView:
        """Pull all dialogs from Telegram and incrementally cache them."""

        account = self.access._find_account(account_id_or_name)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        if not account.is_active:
            raise AdminAccountError("ACCOUNT_INACTIVE", "Telegram 账号已停用")
        pooled_lease = client is None
        telegram_client = client or await self.access._acquire_pooled_client(account)
        chats: list[dict[str, Any]] = []
        avatar_jobs: list[tuple[str, Any, int]] = []
        try:
            async for dialog in telegram_client.iter_dialogs():
                entity = dialog.entity
                kind = self._chat_type(entity)
                if kind is None:
                    continue
                avatar_photo_id = self._chat_photo_id(entity)
                username = getattr(entity, "username", None)
                username_value = f"@{username}" if username else None
                chats.append(
                    {
                        "chat_id": str(entity.id),
                        "chat_type": kind,
                        "title": self._chat_title(entity, dialog),
                        "username": username_value,
                        "has_avatar": avatar_photo_id is not None,
                        "avatar_photo_id": avatar_photo_id,
                    }
                )
                if avatar_photo_id is not None:
                    avatar_jobs.append((str(entity.id), entity, avatar_photo_id))
        except asyncio.CancelledError:
            if pooled_lease:
                await self.access._release_pooled_client(account)
            raise
        except Exception:
            if pooled_lease:
                await self.access._release_pooled_client(account)
            raise AdminAccountError("CHAT_PULL_FAILED", "无法拉取账号对话") from None

        try:
            result = self.database.upsert_account_chats(account.id, chats)
        except asyncio.CancelledError:
            if pooled_lease:
                await self.access._release_pooled_client(account)
            raise
        except Exception:
            if pooled_lease:
                await self.access._release_pooled_client(account)
            raise AdminAccountError("CHAT_PULL_FAILED", "无法保存账号对话") from None
        # The API should return as soon as the dialog snapshot is persisted.
        # Avatar downloads are independent and run in the background.  Only a
        # pooled client is safe to use after this method returns; login-flow
        # clients are disconnected immediately after the initial pull.
        if avatar_jobs and pooled_lease:
            # Transfer the lease to the background downloader; otherwise the
            # client would be disconnected while avatar requests are running.
            self.avatars._schedule_avatar_prefetch(
                account.id, telegram_client, avatar_jobs, account=account
            )
            pooled_lease = False
        if pooled_lease:
            await self.access._release_pooled_client(account)
        return ChatPullView(
            account_id=account.id,
            added=result["added"],
            updated=result["updated"],
            removed=result["removed"],
            total=result["total"],
            synced_at=utc_now(),
        )

    @staticmethod
    def _chat_type(entity: Any) -> Literal["bot", "group", "private"] | None:
        if isinstance(entity, User):
            return "bot" if bool(getattr(entity, "bot", False)) else "private"
        if isinstance(entity, (Chat, Channel)):
            return "group"
        return None

    @staticmethod
    def _chat_photo_id(entity: Any) -> int | None:
        photo = getattr(entity, "photo", None)
        photo_id = getattr(photo, "photo_id", None)
        return photo_id if isinstance(photo_id, int) else None

    @staticmethod
    def _chat_title(entity: Any, dialog: Any) -> str:
        if isinstance(entity, User):
            name = " ".join(
                value
                for value in (
                    getattr(entity, "first_name", None),
                    getattr(entity, "last_name", None),
                )
                if value
            ).strip()
            return name or getattr(entity, "username", None) or str(entity.id)
        return getattr(dialog, "title", None) or getattr(entity, "title", None) or str(entity.id)
