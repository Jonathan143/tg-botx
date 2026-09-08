from __future__ import annotations

import asyncio
import inspect
from typing import Any

from telethon import events

from tg_botx.features.checkin.execution_types import CheckinError
from tg_botx.features.checkin.matching import matches


class TelegramMessageAdapter:
    def __init__(self, client):
        self.client = client

    async def _hydrate_message(self, entity: Any, message: Any) -> Any:
        """Reload a message so event updates carry a usable Telegram client."""

        get_messages = getattr(self.client, "get_messages", None)
        if not callable(get_messages):
            return message
        try:
            refreshed = await get_messages(entity, ids=message.id)
        except Exception:
            return message
        if isinstance(refreshed, (list, tuple)):
            refreshed = refreshed[0] if refreshed else None
        return refreshed or message

    async def _message_buttons(
        self,
        message: Any,
        entity: Any | None = None,
    ) -> list[list[str]] | None:
        """Return visible Telegram button labels while preserving rows."""

        def read_buttons(value: Any) -> list[list[Any]]:
            try:
                buttons = getattr(value, "buttons", None) or []
            except Exception:
                buttons = []
            if buttons:
                return buttons
            # ``Message.buttons`` needs a resolved client/input chat.  The
            # raw reply markup still contains the visible labels, so use it
            # as a rendering-only fallback when that context is unavailable.
            try:
                markup_rows = getattr(getattr(value, "reply_markup", None), "rows", None) or []
                return [
                    raw_row_buttons
                    for raw_row in markup_rows
                    if (raw_row_buttons := getattr(raw_row, "buttons", None))
                ]
            except Exception:
                return []

        buttons = read_buttons(message)
        if not buttons and entity is not None:
            refreshed = await self._hydrate_message(entity, message)
            buttons = read_buttons(refreshed)
        if not buttons:
            get_buttons = getattr(message, "get_buttons", None)
            if callable(get_buttons):
                try:
                    resolved = get_buttons()
                    if inspect.isawaitable(resolved):
                        resolved = await resolved
                    buttons = read_buttons(message) or resolved or []
                except Exception:
                    # Button metadata is supplementary to the message text;
                    # a failed refresh must not turn a successful wait into a
                    # failed task.
                    buttons = []

        rows: list[list[str]] = []
        for row in buttons:
            labels = [
                label
                for label in (str(getattr(button, "text", "") or "") for button in row)
                if label
            ]
            if labels:
                rows.append(labels)
        return rows or None

    @staticmethod
    def _message_type(message: Any) -> str:
        checks = (
            ("sticker", "sticker"),
            ("gif", "animation"),
            ("video_note", "video_note"),
            ("video", "video"),
            ("voice", "voice"),
            ("audio", "audio"),
            ("photo", "photo"),
            ("contact", "contact"),
            ("venue", "venue"),
            ("geo", "location"),
            ("poll", "poll"),
            ("dice", "dice"),
            ("game", "game"),
            ("invoice", "invoice"),
            ("document", "document"),
            ("action", "service"),
        )
        for attribute, label in checks:
            try:
                if getattr(message, attribute, None):
                    return label
            except Exception:
                continue
        if getattr(message, "raw_text", None) is not None:
            return "text"
        return "unknown"

    async def _condition_metadata(self, message: Any, entity: Any) -> dict[str, Any]:
        sender = None
        get_sender = getattr(message, "get_sender", None)
        if callable(get_sender):
            try:
                sender = await get_sender()
            except Exception:
                sender = None
        username = getattr(sender, "username", None) if sender is not None else None
        first_name = getattr(sender, "first_name", None) if sender is not None else None
        last_name = getattr(sender, "last_name", None) if sender is not None else None
        display_name = " ".join(
            part for part in (str(first_name or "").strip(), str(last_name or "").strip()) if part
        )
        if not display_name:
            display_name = str(getattr(sender, "title", "") or username or "")
        is_channel = bool(
            getattr(message, "is_channel", False) or getattr(entity, "broadcast", False)
        )
        is_group = bool(getattr(message, "is_group", False) or getattr(entity, "megagroup", False))
        chat_type = "channel" if is_channel and not is_group else "group" if is_group else "private"
        return {
            "sender.id": getattr(message, "sender_id", None),
            "sender.username": username,
            "sender.display_name": display_name or None,
            "chat.id": getattr(message, "chat_id", None),
            "chat.title": getattr(entity, "title", None),
            "chat.username": getattr(entity, "username", None),
            "chat.type": chat_type,
            "message.id": getattr(message, "id", None),
            "message.date": getattr(message, "date", None),
            "message.text": getattr(message, "raw_text", None) or "",
            "message.type": self._message_type(message),
            "runtime.last_clicked_callback_data_text": None,
            "runtime.last_clicked_callback_data_base64": None,
        }

    async def _latest_message_id(self, entity: Any) -> int:
        message = await self.client.get_messages(entity, limit=1)
        return message[0].id if message else 0

    async def _wait_for_message(
        self,
        entity: Any,
        bot_id: int | None,
        baseline: int,
        step: dict[str, Any],
        timeout: int,
        editable_message_ids: set[int] | None = None,
        editable_message_texts: dict[int, str] | None = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        editable_message_ids = editable_message_ids or set()
        editable_message_texts = editable_message_texts or {}

        async def inspect_message(message: Any) -> None:
            """Resolve the waiter when a new matching bot message is found.

            A response can arrive between the previous step and event-handler
            registration.  Keeping the same matching logic in one function
            lets us catch up from message history after registering handlers.
            """
            if future.done():
                return
            is_editable_message = message.id in editable_message_ids
            if message.id <= baseline and not is_editable_message:
                return
            if is_editable_message and message.raw_text == editable_message_texts.get(message.id):
                return
            if bot_id is not None:
                if getattr(message, "sender_id", None) != bot_id:
                    return
            else:
                sender = await message.get_sender()
                if not getattr(sender, "bot", False):
                    return
            text = message.raw_text or ""
            if matches(text, step.get("failure")):
                buttons = await self._message_buttons(message, entity)
                future.set_exception(CheckinError("机器人返回失败消息", text, buttons))
                return
            success_rule = step.get("success")
            if success_rule is None or matches(text, success_rule):
                future.set_result(message)

        async def handler(event: Any) -> None:
            await inspect_message(event.message)

        self.client.add_event_handler(handler, events.NewMessage(chats=entity))
        self.client.add_event_handler(handler, events.MessageEdited(chats=entity))
        try:
            # Catch responses that were sent before the event handlers were
            # attached.  Telegram returns newest messages first, so inspect in
            # chronological order to preserve the event-handler semantics.
            min_id = max(0, baseline - 1) if editable_message_ids else baseline
            messages = await self.client.get_messages(entity, limit=50, min_id=min_id)
            for message in reversed(messages or []):
                await inspect_message(message)
                if future.done():
                    break
            if future.done():
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.client.remove_event_handler(handler, events.NewMessage(chats=entity))
            self.client.remove_event_handler(handler, events.MessageEdited(chats=entity))

    async def _click(self, message: Any, button: Any, selector: dict[str, Any]) -> None:
        # Telethon's ``Message.click`` returns ``None`` without raising when
        # the message is not attached to a client.  Treat that state as an
        # actionable error so a workflow cannot report a false success.
        if hasattr(message, "_client") and getattr(message, "_client", None) is None:
            raise CheckinError("Telegram 消息未完成加载，无法点击按钮")
        if selector.get("callback_data") is not None:
            value = selector["callback_data"]
            await message.click(data=value.encode() if isinstance(value, str) else value)
            return
        if selector.get("row") is not None and selector.get("column") is not None:
            await message.click(selector["row"], selector["column"])
            return
        # A text match identifies a concrete button object, but resolving it
        # again through ``Message.click(text=...)`` can lose the opaque
        # callback payload (especially when labels contain emoji or invisible
        # formatting).  Send the payload from the matched button directly so
        # Telegram receives exactly the callback data that was inspected.
        button_data = getattr(button, "data", None)
        if button_data is not None:
            await message.click(data=button_data)
            return
        # Resolve the action from the refreshed message instead of invoking a
        # button object retained from an event update.  The latter may not
        # carry the input chat/client and can silently do nothing.
        await message.click(text=getattr(button, "text", ""))
