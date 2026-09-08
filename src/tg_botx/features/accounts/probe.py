from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import events

from tg_botx.features.accounts.models import (
    AdminAccountError,
    MessageProbeView,
)

logger = logging.getLogger(__name__)


class MessageProbe:
    def __init__(self, access):
        self.access = access

    async def probe_message(
        self,
        account_id_or_name: str,
        target: str,
        text: str,
        *,
        timeout_seconds: int = 30,
    ) -> MessageProbeView:
        """Send a probe command and return the next bot message's buttons."""

        account = self.access._find_account(account_id_or_name)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        if not account.is_active:
            raise AdminAccountError("ACCOUNT_INACTIVE", "Telegram 账号已停用")
        if not target.strip():
            raise AdminAccountError("CHAT_TARGET_INVALID", "目标聊天不能为空")
        if not text:
            raise AdminAccountError("MESSAGE_TEXT_INVALID", "发送内容不能为空")
        if timeout_seconds < 1 or timeout_seconds > 120:
            raise AdminAccountError("MESSAGE_TIMEOUT_INVALID", "等待时间必须在 1–120 秒之间")

        client = await self.access._acquire_pooled_client(account)
        try:
            entity = await client.get_entity(target.strip())
            sent = await client.send_message(entity, text)
            baseline = int(getattr(sent, "id", 0) or 0)
            loop = asyncio.get_running_loop()
            future: asyncio.Future[Any] = loop.create_future()

            async def inspect(message: Any) -> None:
                if future.done() or int(getattr(message, "id", 0) or 0) <= baseline:
                    return
                target_id = getattr(entity, "id", None)
                sender_id = getattr(message, "sender_id", None)
                if (
                    target_id is not None
                    and getattr(entity, "bot", False)
                    and sender_id != target_id
                ):
                    return
                future.set_result(message)

            async def handler(event: Any) -> None:
                await inspect(event.message)

            client.add_event_handler(handler, events.NewMessage(chats=entity))
            try:
                messages = await client.get_messages(entity, limit=20, min_id=baseline)
                for message in reversed(messages or []):
                    await inspect(message)
                    if future.done():
                        break
                try:
                    received = await asyncio.wait_for(future, timeout=timeout_seconds)
                except TimeoutError:
                    raise AdminAccountError("MESSAGE_WAIT_TIMEOUT", "等待机器人回复超时") from None
            finally:
                client.remove_event_handler(handler, events.NewMessage(chats=entity))
        except AdminAccountError:
            await self.access._release_pooled_client(account)
            raise
        except asyncio.CancelledError:
            await self.access._release_pooled_client(account)
            raise
        except Exception:
            await self.access._release_pooled_client(account)
            raise AdminAccountError("MESSAGE_PROBE_FAILED", "发送指令或读取回复失败") from None

        try:
            refreshed = await client.get_messages(entity, ids=received.id)
            if isinstance(refreshed, (list, tuple)):
                refreshed = refreshed[0] if refreshed else None
            if refreshed is not None:
                received = refreshed
        except asyncio.CancelledError:
            await self.access._release_pooled_client(account)
            raise
        except Exception:
            pass
        await self.access._release_pooled_client(account)

        buttons = getattr(received, "buttons", None) or []
        if not buttons:
            markup_rows = getattr(getattr(received, "reply_markup", None), "rows", None) or []
            buttons = [getattr(row, "buttons", None) or [] for row in markup_rows]
        rows: list[dict[str, Any]] = []
        for row_index, row in enumerate(buttons):
            for column_index, button in enumerate(row):
                callback_data = getattr(button, "data", None)
                if isinstance(callback_data, bytes):
                    callback_value = callback_data.decode("utf-8", errors="replace")
                elif callback_data is None:
                    callback_value = None
                else:
                    callback_value = str(callback_data)
                rows.append(
                    {
                        "row": row_index,
                        "column": column_index,
                        "text": str(getattr(button, "text", "") or ""),
                        "callbackData": callback_value,
                    }
                )
        return MessageProbeView(
            message_id=int(getattr(received, "id", 0) or 0),
            text=str(getattr(received, "raw_text", "") or ""),
            buttons=tuple(rows),
        )
