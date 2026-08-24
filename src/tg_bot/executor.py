from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from telethon import TelegramClient, events

from tg_bot.matching import match_button, matches


class CheckinError(RuntimeError):
    pass


class CheckinExecutor:
    def __init__(self, client: TelegramClient, is_cancelled: Callable[[], bool] | None = None):
        self.client = client
        self.is_cancelled = is_cancelled or (lambda: False)

    async def execute(self, task: Any) -> None:
        entity = await self.client.get_entity(task.target)
        bot = await self.client.get_entity(task.target)
        baseline = await self._latest_message_id(entity)
        current_message = None

        for index, step in enumerate(task.config["steps"]):
            if self.is_cancelled():
                raise asyncio.CancelledError
            kind = step["type"]
            try:
                if kind == "send_message":
                    current_message = await self.client.send_message(entity, step["text"])
                    baseline = max(baseline, current_message.id)
                elif kind == "wait_message":
                    current_message = await self._wait_for_message(
                        entity=entity,
                        bot_id=bot.id if getattr(bot, "bot", False) else None,
                        baseline=baseline,
                        step=step,
                        timeout=step.get("timeout_seconds", 60),
                    )
                    baseline = max(baseline, current_message.id)
                elif kind == "click_button":
                    if current_message is None:
                        raise CheckinError("点击按钮步骤前没有可用的机器人消息")
                    button = match_button(current_message, step)
                    await self._click(current_message, button, step)
                else:
                    raise CheckinError(f"不支持的步骤类型：{kind}")
            except asyncio.TimeoutError as exc:
                raise CheckinError(f"步骤 {index + 1} 等待超时") from exc
            except CheckinError:
                raise
            except Exception as exc:
                raise CheckinError(f"步骤 {index + 1} 执行失败：{exc}") from exc

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
    ) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        async def handler(event: Any) -> None:
            message = event.message
            if message.id <= baseline or future.done():
                return
            if bot_id is not None:
                if event.sender_id != bot_id:
                    return
            else:
                sender = await event.get_sender()
                if not getattr(sender, "bot", False):
                    return
            text = message.raw_text or ""
            if matches(text, step.get("failure")):
                future.set_exception(CheckinError(f"机器人返回失败消息：{text[:200]}"))
                return
            success_rule = step.get("success")
            if success_rule is None or matches(text, success_rule):
                future.set_result(message)

        self.client.add_event_handler(handler, events.NewMessage(chats=entity))
        self.client.add_event_handler(handler, events.MessageEdited(chats=entity))
        try:
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


async def run_with_retries(executor: CheckinExecutor, task: Any) -> tuple[bool, str | None, int]:
    retry = task.config.get("retry", {})
    max_attempts = retry.get("max_attempts", 3)
    backoff = retry.get("backoff_seconds", [30, 60, 120])
    error: str | None = None
    for attempt in range(1, max_attempts + 1):
        if executor.is_cancelled():
            raise asyncio.CancelledError
        try:
            await executor.execute(task)
            return True, None, attempt
        except Exception as exc:
            error = str(exc)
            if attempt < max_attempts:
                await asyncio.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
    return False, error, max_attempts
