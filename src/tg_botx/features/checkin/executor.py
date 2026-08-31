from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events

from tg_botx.features.checkin.condition import (
    ConditionEvaluationError,
    ConditionInput,
    ConditionVariable,
    callback_data_values,
    normalize_legacy_condition,
    render_matcher_templates,
    render_template,
    select_branch,
)
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


@dataclass(slots=True)
class ExecutionContext:
    entity: Any
    bot_id: int | None
    timezone: ZoneInfo
    baseline: int
    current_message: Any = None
    last_wait_message: Any = None
    last_wait_text: str | None = None
    last_wait_metadata: dict[str, Any] = field(default_factory=dict)
    last_clicked_callback_data_text: str | None = None
    last_clicked_callback_data_base64: str | None = None
    bot_response: str | None = None
    bot_buttons: list[list[str]] | None = None
    editable_message_ids: set[int] = field(default_factory=set)
    editable_message_texts: dict[int, str] = field(default_factory=dict)
    variables: dict[str, ConditionVariable] = field(default_factory=dict)


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
        index: int | None,
        status: Literal["pending", "running", "success", "failed", "skipped"],
        error: str | None = None,
        duration_ms: int | None = None,
        node_id: str | None = None,
        step_path: str | None = None,
        selected_branch: dict[str, Any] | None = None,
        condition_variables: list[dict[str, Any]] | None = None,
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
        if len(positional) >= 8 or has_varargs:
            await callback(
                index,
                status,
                error,
                duration_ms,
                node_id,
                step_path,
                selected_branch,
                condition_variables,
            )
        elif len(positional) >= 4:
            await callback(index, status, error, duration_ms)
        else:
            # Preserve compatibility with the original three-argument hook.
            await callback(index, status, error)

    async def report_step_response(
        self,
        index: int | None,
        response: str,
        buttons: list[list[str]] | None = None,
        node_id: str | None = None,
        step_path: str | None = None,
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
        if len(positional) >= 5 or has_varargs:
            await callback(index, response, buttons, node_id, step_path)
        elif len(positional) >= 3:
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

    @staticmethod
    def _step_identity(
        step: dict[str, Any], step_path: str, top_index: int | None
    ) -> tuple[int | None, str | None, str]:
        node_id = step.get("node_id") or step.get("nodeId")
        return top_index, str(node_id) if node_id else None, step_path

    async def _mark_steps_skipped(
        self,
        steps: list[dict[str, Any]],
        path_prefix: str,
    ) -> None:
        for index, nested in enumerate(steps):
            step_path = f"{path_prefix}[{index}]"
            _, node_id, resolved_path = self._step_identity(nested, step_path, None)
            await self.report_step_status(
                None,
                "skipped",
                node_id=node_id,
                step_path=resolved_path,
            )
            if nested.get("type") == "condition":
                normalized = normalize_legacy_condition(nested)
                for branch_index, branch in enumerate(normalized.get("branches", [])):
                    await self._mark_steps_skipped(
                        branch.get("steps") or [],
                        f"{step_path}.branches[{branch_index}].steps",
                    )

    async def execute(self, task: Any) -> str | None:
        entity = await self.client.get_entity(task.target)
        bot = await self.client.get_entity(task.target)
        timezone_name = getattr(task, "timezone", None) or task.config.get("schedule", {}).get(
            "timezone", "Asia/Shanghai"
        )
        context = ExecutionContext(
            entity=entity,
            bot_id=bot.id if getattr(bot, "bot", False) else None,
            timezone=ZoneInfo(str(timezone_name)),
            baseline=await self._latest_message_id(entity),
        )
        await self._execute_steps(task.config["steps"], context, "steps", top_level=True)
        return context.bot_response

    async def _execute_steps(
        self,
        steps: list[dict[str, Any]],
        context: ExecutionContext,
        path_prefix: str,
        *,
        top_level: bool = False,
    ) -> None:
        for sequence_index, original_step in enumerate(steps):
            step_path = f"{path_prefix}[{sequence_index}]"
            top_index = sequence_index if top_level else None
            step = (
                normalize_legacy_condition(original_step)
                if original_step.get("type") == "condition"
                else original_step
            )
            index, node_id, resolved_path = self._step_identity(original_step, step_path, top_index)
            if self.is_cancelled():
                raise asyncio.CancelledError
            kind = step["type"]
            await self.report_step_status(
                index,
                "running",
                node_id=node_id,
                step_path=resolved_path,
            )
            step_started_at = time.perf_counter()

            def step_duration_ms(started_at: float = step_started_at) -> int:
                return max(0, round((time.perf_counter() - started_at) * 1000))

            step_response_reported = False
            condition_reported = False
            step_label = f"步骤 {index + 1}" if index is not None else f"节点 {resolved_path}"
            try:
                if kind == "send_message":
                    text = render_template(str(step["text"]), context.variables)
                    context.current_message = await self.client.send_message(context.entity, text)
                    context.baseline = max(context.baseline, context.current_message.id)
                    context.editable_message_ids.clear()
                    context.editable_message_texts.clear()
                elif kind == "wait_message":
                    rendered_step = {
                        **step,
                        "success": render_matcher_templates(step.get("success"), context.variables),
                        "failure": render_matcher_templates(step.get("failure"), context.variables),
                    }
                    context.current_message = await self._wait_for_message(
                        entity=context.entity,
                        bot_id=context.bot_id,
                        baseline=context.baseline,
                        step=rendered_step,
                        timeout=step.get("timeout_seconds", 60),
                        editable_message_ids=context.editable_message_ids,
                        editable_message_texts=context.editable_message_texts,
                    )
                    context.current_message = await self._hydrate_message(
                        context.entity, context.current_message
                    )
                    context.last_wait_message = context.current_message
                    context.last_wait_text = context.current_message.raw_text or ""
                    context.last_wait_metadata = await self._condition_metadata(
                        context.current_message, context.entity
                    )
                    context.last_wait_metadata["runtime.last_clicked_callback_data_text"] = (
                        context.last_clicked_callback_data_text
                    )
                    context.last_wait_metadata["runtime.last_clicked_callback_data_base64"] = (
                        context.last_clicked_callback_data_base64
                    )
                    context.bot_response = context.last_wait_text
                    context.bot_buttons = await self._message_buttons(
                        context.current_message, context.entity
                    )
                    if context.bot_buttons is None:
                        await self.report_step_response(
                            index,
                            context.bot_response,
                            node_id=node_id,
                            step_path=resolved_path,
                        )
                    else:
                        await self.report_step_response(
                            index,
                            context.bot_response,
                            context.bot_buttons,
                            node_id=node_id,
                            step_path=resolved_path,
                        )
                    step_response_reported = True
                    context.baseline = max(context.baseline, context.current_message.id)
                    context.editable_message_ids.clear()
                    context.editable_message_texts.clear()
                elif kind == "click_button":
                    if context.current_message is None:
                        raise CheckinError("点击按钮步骤前没有可用的机器人消息")
                    rendered_step = dict(step)
                    for field in ("text", "text_contains", "callback_data"):
                        if isinstance(rendered_step.get(field), str):
                            rendered_step[field] = render_template(
                                rendered_step[field], context.variables
                            )
                    context.current_message = await self._hydrate_message(
                        context.entity, context.current_message
                    )
                    if not getattr(context.current_message, "buttons", None):
                        get_buttons = getattr(context.current_message, "get_buttons", None)
                        if callable(get_buttons):
                            await get_buttons()
                    button = match_button(context.current_message, rendered_step)
                    context.editable_message_ids = {context.current_message.id}
                    context.editable_message_texts = {
                        context.current_message.id: context.current_message.raw_text or ""
                    }
                    await self._click(context.current_message, button, rendered_step)
                    callback_value = getattr(button, "data", None)
                    if callback_value is None:
                        callback_value = rendered_step.get("callback_data")
                    callback_text, callback_base64 = callback_data_values(callback_value)
                    context.last_clicked_callback_data_text = callback_text
                    context.last_clicked_callback_data_base64 = callback_base64
                    context.last_wait_metadata["runtime.last_clicked_callback_data_text"] = (
                        callback_text
                    )
                    context.last_wait_metadata["runtime.last_clicked_callback_data_base64"] = (
                        callback_base64
                    )
                elif kind == "condition":
                    selected_index, selected, extraction_results = select_branch(
                        step,
                        ConditionInput(
                            message_text=context.last_wait_text,
                            metadata=context.last_wait_metadata,
                            timezone=context.timezone,
                        ),
                        context.variables,
                    )
                    selected_branch = {
                        "index": selected_index,
                        "kind": selected.get("kind"),
                        "name": selected.get("name"),
                    }
                    await self.report_step_status(
                        index,
                        "success",
                        duration_ms=step_duration_ms(),
                        node_id=node_id,
                        step_path=resolved_path,
                        selected_branch=selected_branch,
                        condition_variables=extraction_results,
                    )
                    condition_reported = True
                    for branch_index, branch in enumerate(step.get("branches", [])):
                        if branch_index != selected_index:
                            await self._mark_steps_skipped(
                                branch.get("steps") or [],
                                f"{resolved_path}.branches[{branch_index}].steps",
                            )
                    await self._execute_steps(
                        selected.get("steps") or [],
                        context,
                        f"{resolved_path}.branches[{selected_index}].steps",
                    )
                else:
                    raise CheckinError(f"不支持的步骤类型：{kind}")
            except asyncio.CancelledError:
                await self.report_step_status(
                    index,
                    "failed",
                    "任务已取消",
                    duration_ms=step_duration_ms(),
                    node_id=node_id,
                    step_path=resolved_path,
                )
                raise
            except TimeoutError as exc:
                error = CheckinError(
                    f"{step_label} 等待超时", context.bot_response, context.bot_buttons
                )
                await self.report_step_status(
                    index,
                    "failed",
                    str(error),
                    duration_ms=step_duration_ms(),
                    node_id=node_id,
                    step_path=resolved_path,
                )
                raise error from exc
            except ConditionEvaluationError as exc:
                error = CheckinError(
                    f"{step_label} 条件判断失败：{exc}",
                    context.bot_response,
                    context.bot_buttons,
                )
                await self.report_step_status(
                    index,
                    "failed",
                    str(error),
                    duration_ms=step_duration_ms(),
                    node_id=node_id,
                    step_path=resolved_path,
                )
                raise error from exc
            except CheckinError as exc:
                if exc.bot_response is not None and not step_response_reported:
                    context.bot_response = exc.bot_response
                    if exc.bot_buttons is None:
                        await self.report_step_response(
                            index,
                            context.bot_response,
                            node_id=node_id,
                            step_path=resolved_path,
                        )
                    else:
                        await self.report_step_response(
                            index,
                            context.bot_response,
                            exc.bot_buttons,
                            node_id=node_id,
                            step_path=resolved_path,
                        )
                if exc.bot_response is None:
                    exc.bot_response = context.bot_response
                await self.report_step_status(
                    index,
                    "failed",
                    str(exc),
                    duration_ms=step_duration_ms(),
                    node_id=node_id,
                    step_path=resolved_path,
                )
                raise
            except Exception as exc:
                error = CheckinError(
                    f"{step_label} 执行失败：{exc}", context.bot_response, context.bot_buttons
                )
                await self.report_step_status(
                    index,
                    "failed",
                    str(error),
                    duration_ms=step_duration_ms(),
                    node_id=node_id,
                    step_path=resolved_path,
                )
                raise error from exc
            if not condition_reported:
                await self.report_step_status(
                    index,
                    "success",
                    duration_ms=step_duration_ms(),
                    node_id=node_id,
                    step_path=resolved_path,
                )

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
