from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from telethon import TelegramClient, events

from tg_botx.features.checkin.matching import match_button, matches


class CheckinError(RuntimeError):
    def __init__(
        self,
        message: str,
        bot_response: str | None = None,
        bot_buttons: list[list[str]] | None = None,
    ):
        super().__init__(message)
        self.bot_response = bot_response
        self.bot_buttons = bot_buttons


class CheckinExecutor:
    def __init__(
        self,
        client: TelegramClient,
        is_cancelled: Callable[[], bool] | None = None,
        on_attempt: Callable[[int], Awaitable[None]] | None = None,
        on_step_status: Callable[..., Awaitable[None]] | None = None,
        on_step_response: Callable[..., Awaitable[None]] | None = None,
    ):
        self.client = client
        self.is_cancelled = is_cancelled or (lambda: False)
        self.on_attempt = on_attempt
        self.on_step_status = on_step_status
        self.on_step_response = on_step_response

    async def begin_attempt(self, attempt: int) -> None:
        if self.on_attempt is not None:
            await self.on_attempt(attempt)

    async def report_step_status(
        self,
        index: int,
        status: Literal["running", "success", "failed"],
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        callback = self.on_step_status
        if callback is None:
            return
        try:
            parameters = inspect.signature(callback).parameters.values()
            positional = [
                parameter
                for parameter in parameters
                if parameter.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            has_varargs = any(
                parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
            )
        except (TypeError, ValueError):
            has_varargs = True
            positional = []
        if len(positional) >= 4 or (duration_ms is not None and has_varargs):
            await callback(index, status, error, duration_ms)
        else:
            # Preserve compatibility with the original three-argument hook.
            await callback(index, status, error)

    async def report_step_response(
        self,
        index: int,
        response: str,
        buttons: list[list[str]] | None = None,
    ) -> None:
        callback = self.on_step_response
        if callback is None:
            return
        try:
            parameters = inspect.signature(callback).parameters.values()
            positional = [
                parameter
                for parameter in parameters
                if parameter.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            has_varargs = any(
                parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
            )
        except (TypeError, ValueError):
            # Some extension callables do not expose a signature.  The new
            # callback form is the safest default for those callables.
            has_varargs = True
            positional = []
        if len(positional) >= 3 or (buttons is not None and has_varargs):
            await callback(index, response, buttons)
        else:
            # Keep compatibility with integrations using the original
            # two-argument callback while allowing the runtime to consume the
            # optional button rows.
            await callback(index, response)

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

    async def execute(self, task: Any) -> str | None:
        entity = await self.client.get_entity(task.target)
        bot = await self.client.get_entity(task.target)
        baseline = await self._latest_message_id(entity)
        current_message = None
        bot_response: str | None = None
        bot_buttons: list[list[str]] | None = None
        editable_message_ids: set[int] = set()
        editable_message_texts: dict[int, str] = {}

        for index, step in enumerate(task.config["steps"]):
            if self.is_cancelled():
                raise asyncio.CancelledError
            kind = step["type"]
            await self.report_step_status(index, "running")
            step_started_at = time.perf_counter()

            def step_duration_ms() -> int:
                return max(0, round((time.perf_counter() - step_started_at) * 1000))

            step_response_reported = False
            try:
                if kind == "send_message":
                    current_message = await self.client.send_message(entity, step["text"])
                    baseline = max(baseline, current_message.id)
                    editable_message_ids.clear()
                    editable_message_texts.clear()
                elif kind == "wait_message":
                    current_message = await self._wait_for_message(
                        entity=entity,
                        bot_id=bot.id if getattr(bot, "bot", False) else None,
                        baseline=baseline,
                        step=step,
                        timeout=step.get("timeout_seconds", 60),
                        editable_message_ids=editable_message_ids,
                        editable_message_texts=editable_message_texts,
                    )
                    current_message = await self._hydrate_message(entity, current_message)
                    bot_response = current_message.raw_text or ""
                    bot_buttons = await self._message_buttons(current_message, entity)
                    if bot_buttons is None:
                        await self.report_step_response(index, bot_response)
                    else:
                        await self.report_step_response(index, bot_response, bot_buttons)
                    step_response_reported = True
                    baseline = max(baseline, current_message.id)
                    editable_message_ids.clear()
                    editable_message_texts.clear()
                elif kind == "click_button":
                    if current_message is None:
                        raise CheckinError("点击按钮步骤前没有可用的机器人消息")
                    # A message received from an update can still carry an
                    # incomplete Telegram client/input-chat context.  Reload
                    # it immediately before matching/clicking so the click
                    # request is sent through the active client rather than
                    # being silently ignored by ``Message.click``.
                    current_message = await self._hydrate_message(entity, current_message)
                    # ``Message.buttons`` can be empty when Telethon has not
                    # resolved the input chat/sender yet (notably for messages
                    # received through an update).  ``get_buttons`` performs
                    # the documented asynchronous fallback and caches the
                    # resolved rows for the matcher.
                    if not getattr(current_message, "buttons", None):
                        get_buttons = getattr(current_message, "get_buttons", None)
                        if callable(get_buttons):
                            await get_buttons()
                    button = match_button(current_message, step)
                    editable_message_ids = {current_message.id}
                    editable_message_texts = {current_message.id: current_message.raw_text or ""}
                    await self._click(current_message, button, step)
                else:
                    raise CheckinError(f"不支持的步骤类型：{kind}")
            except asyncio.CancelledError:
                await self.report_step_status(
                    index,
                    "failed",
                    "任务已取消",
                    duration_ms=step_duration_ms(),
                )
                raise
            except asyncio.TimeoutError as exc:
                error = CheckinError(f"步骤 {index + 1} 等待超时", bot_response, bot_buttons)
                await self.report_step_status(
                    index,
                    "failed",
                    str(error),
                    duration_ms=step_duration_ms(),
                )
                raise error from exc
            except CheckinError as exc:
                if exc.bot_response is not None and not step_response_reported:
                    bot_response = exc.bot_response
                    if exc.bot_buttons is None:
                        await self.report_step_response(index, bot_response)
                    else:
                        await self.report_step_response(index, bot_response, exc.bot_buttons)
                if exc.bot_response is None:
                    exc.bot_response = bot_response
                await self.report_step_status(
                    index,
                    "failed",
                    str(exc),
                    duration_ms=step_duration_ms(),
                )
                raise
            except Exception as exc:
                error = CheckinError(
                    f"步骤 {index + 1} 执行失败：{exc}", bot_response, bot_buttons
                )
                await self.report_step_status(
                    index,
                    "failed",
                    str(error),
                    duration_ms=step_duration_ms(),
                )
                raise error from exc
            await self.report_step_status(index, "success", duration_ms=step_duration_ms())
        return bot_response

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


async def run_with_retries(
    executor: CheckinExecutor, task: Any
) -> tuple[bool, str | None, int, str | None]:
    retry = task.config.get("retry", {})
    max_attempts = retry.get("max_attempts", 3)
    backoff = retry.get("backoff_seconds", [30, 60, 120])
    error: str | None = None
    bot_response: str | None = None
    for attempt in range(1, max_attempts + 1):
        if executor.is_cancelled():
            raise asyncio.CancelledError
        await executor.begin_attempt(attempt)
        try:
            bot_response = await executor.execute(task)
            return True, None, attempt, bot_response
        except Exception as exc:
            error = str(exc)
            bot_response = getattr(exc, "bot_response", None)
            if attempt < max_attempts:
                delay = backoff[min(attempt - 1, len(backoff) - 1)] if backoff else 0
                await asyncio.sleep(delay)
    return False, error, max_attempts, bot_response
