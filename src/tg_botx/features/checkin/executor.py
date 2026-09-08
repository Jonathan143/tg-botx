from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from telethon import TelegramClient

from tg_botx.features.checkin.condition import (
    ConditionEvaluationError,
    normalize_legacy_condition,
)
from tg_botx.features.checkin.execution_types import CheckinError as CheckinError
from tg_botx.features.checkin.execution_types import ExecutionContext as ExecutionContext
from tg_botx.features.checkin.execution_types import StepReport
from tg_botx.features.checkin.steps import (
    click_button,
    condition,
    extract_variable,
    http_request,
    send_message,
    wait_message,
)
from tg_botx.integrations.checkin_messages import TelegramMessageAdapter

STEP_HANDLERS = {
    "send_message": send_message.execute,
    "wait_message": wait_message.execute,
    "click_button": click_button.execute,
    "http_request": http_request.execute,
    "extract_variable": extract_variable.execute,
    "condition": condition.execute,
}


def callback_signature(callback: Callable[..., Awaitable[None]] | None) -> tuple[int, bool]:
    if callback is None:
        return 0, False
    try:
        parameters = tuple(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return 0, True
    return (
        sum(
            parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            for parameter in parameters
        ),
        any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters),
    )


class CheckinExecutor:
    def __init__(
        self,
        client: TelegramClient,
        is_cancelled: Callable[[], bool] | None = None,
        on_attempt: Callable[[int], Awaitable[None]] | None = None,
        on_step_status: Callable[..., Awaitable[None]] | None = None,
        on_step_response: Callable[..., Awaitable[None]] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self.client = client
        self.messages = TelegramMessageAdapter(client)
        self.is_cancelled = is_cancelled or (lambda: False)
        self.on_attempt = on_attempt
        self.on_step_status = on_step_status
        self.on_step_response = on_step_response
        self._status_signature = callback_signature(on_step_status)
        self._response_signature = callback_signature(on_step_response)

    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient()
        return self._http_client

    async def close(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

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
        positional_count, has_varargs = self._status_signature
        if positional_count >= 8 or has_varargs:
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
        elif positional_count >= 4:
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
        positional_count, has_varargs = self._response_signature
        if positional_count >= 5 or has_varargs:
            await callback(index, response, buttons, node_id, step_path)
        elif positional_count >= 3:
            await callback(index, response, buttons)
        else:
            # Keep compatibility with integrations using the original
            # two-argument callback while allowing the runtime to consume the
            # optional button rows.
            await callback(index, response)

    async def _hydrate_message(self, entity: Any, message: Any) -> Any:
        return await self.messages._hydrate_message(entity, message)

    async def _message_buttons(
        self, message: Any, entity: Any | None = None
    ) -> list[list[str]] | None:
        return await self.messages._message_buttons(message, entity)

    @staticmethod
    def _message_type(message: Any) -> str:
        return TelegramMessageAdapter._message_type(message)

    async def _condition_metadata(self, message: Any, entity: Any) -> dict[str, Any]:
        return await self.messages._condition_metadata(message, entity)

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
        bot = entity
        timezone_name = getattr(task, "timezone", None) or task.config.get("schedule", {}).get(
            "timezone", "Asia/Shanghai"
        )
        context = ExecutionContext(
            entity=entity,
            bot_id=bot.id if getattr(bot, "bot", False) else None,
            timezone=ZoneInfo(str(timezone_name)),
            baseline=await self._latest_message_id(entity),
        )
        try:
            await self._execute_steps(task.config["steps"], context, "steps", top_level=True)
            return context.bot_response
        finally:
            await self.close()

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

            report = StepReport()
            step_label = f"步骤 {index + 1}" if index is not None else f"节点 {resolved_path}"
            try:
                handler = STEP_HANDLERS.get(kind)
                if handler is None:
                    raise CheckinError(f"不支持的步骤类型：{kind}")
                await handler(
                    self, step, context, index, node_id, resolved_path, step_duration_ms, report
                )
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
                    f"{step_label} {'变量提取失败' if kind == 'extract_variable' else '条件判断失败'}：{exc}",
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
                if exc.bot_response is not None and not report.response_reported:
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
            if not report.condition_reported:
                await self.report_step_status(
                    index,
                    "success",
                    duration_ms=step_duration_ms(),
                    node_id=node_id,
                    step_path=resolved_path,
                )

    async def _latest_message_id(self, entity: Any) -> int:
        return await self.messages._latest_message_id(entity)

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
        return await self.messages._wait_for_message(
            entity, bot_id, baseline, step, timeout, editable_message_ids, editable_message_texts
        )

    async def _click(self, message: Any, button: Any, selector: dict[str, Any]) -> None:
        return await self.messages._click(message, button, selector)


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
