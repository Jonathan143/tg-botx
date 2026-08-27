from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from telethon import TelegramClient, events

from tg_botx.features.checkin.matching import match_button, matches


class CheckinError(RuntimeError):
    def __init__(self, message: str, bot_response: str | None = None):
        super().__init__(message)
        self.bot_response = bot_response


class CheckinExecutor:
    def __init__(
        self,
        client: TelegramClient,
        is_cancelled: Callable[[], bool] | None = None,
        on_attempt: Callable[[int], Awaitable[None]] | None = None,
        on_step_status: Callable[
            [int, Literal["running", "success", "failed"], str | None], Awaitable[None]
        ]
        | None = None,
    ):
        self.client = client
        self.is_cancelled = is_cancelled or (lambda: False)
        self.on_attempt = on_attempt
        self.on_step_status = on_step_status

    async def begin_attempt(self, attempt: int) -> None:
        if self.on_attempt is not None:
            await self.on_attempt(attempt)

    async def report_step_status(
        self,
        index: int,
        status: Literal["running", "success", "failed"],
        error: str | None = None,
    ) -> None:
        if self.on_step_status is not None:
            await self.on_step_status(index, status, error)

    async def execute(self, task: Any) -> str | None:
        entity = await self.client.get_entity(task.target)
        bot = await self.client.get_entity(task.target)
        baseline = await self._latest_message_id(entity)
        current_message = None
        bot_response: str | None = None
        editable_message_ids: set[int] = set()
        editable_message_texts: dict[int, str] = {}

        for index, step in enumerate(task.config["steps"]):
            if self.is_cancelled():
                raise asyncio.CancelledError
            kind = step["type"]
            await self.report_step_status(index, "running")
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
                    bot_response = current_message.raw_text or ""
                    baseline = max(baseline, current_message.id)
                    editable_message_ids.clear()
                    editable_message_texts.clear()
                elif kind == "click_button":
                    if current_message is None:
                        raise CheckinError("点击按钮步骤前没有可用的机器人消息")
                    button = match_button(current_message, step)
                    editable_message_ids = {current_message.id}
                    editable_message_texts = {current_message.id: current_message.raw_text or ""}
                    await self._click(current_message, button, step)
                else:
                    raise CheckinError(f"不支持的步骤类型：{kind}")
            except asyncio.CancelledError:
                await self.report_step_status(index, "failed", "任务已取消")
                raise
            except asyncio.TimeoutError as exc:
                error = CheckinError(f"步骤 {index + 1} 等待超时", bot_response)
                await self.report_step_status(index, "failed", str(error))
                raise error from exc
            except CheckinError as exc:
                if exc.bot_response is None:
                    exc.bot_response = bot_response
                await self.report_step_status(index, "failed", str(exc))
                raise
            except Exception as exc:
                error = CheckinError(f"步骤 {index + 1} 执行失败：{exc}", bot_response)
                await self.report_step_status(index, "failed", str(error))
                raise error from exc
            await self.report_step_status(index, "success")
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
                future.set_exception(CheckinError("机器人返回失败消息", text))
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
        if selector.get("callback_data") is not None:
            value = selector["callback_data"]
            await message.click(data=value.encode() if isinstance(value, str) else value)
            return
        if selector.get("row") is not None and selector.get("column") is not None:
            await message.click(selector["row"], selector["column"])
            return
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
