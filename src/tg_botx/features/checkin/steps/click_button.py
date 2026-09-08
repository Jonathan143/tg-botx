from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tg_botx.features.checkin.condition import (
    callback_data_values,
    render_template,
)
from tg_botx.features.checkin.execution_types import CheckinError as CheckinError
from tg_botx.features.checkin.execution_types import ExecutionContext as ExecutionContext
from tg_botx.features.checkin.execution_types import StepReport
from tg_botx.features.checkin.matching import match_button

if TYPE_CHECKING:
    from tg_botx.features.checkin.executor import CheckinExecutor


async def execute(
    self: CheckinExecutor,
    step: dict[str, Any],
    context: ExecutionContext,
    index: int | None,
    node_id: str | None,
    resolved_path: str,
    step_duration_ms: Callable[[], int],
    report: StepReport,
) -> None:
    if context.current_message is None:
        raise CheckinError("点击按钮步骤前没有可用的机器人消息")
    rendered_step = dict(step)
    for field in ("text", "text_contains", "callback_data"):
        if isinstance(rendered_step.get(field), str):
            rendered_step[field] = render_template(rendered_step[field], context.variables)
    context.current_message = await self._hydrate_message(context.entity, context.current_message)
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
    context.last_wait_metadata["runtime.last_clicked_callback_data_text"] = callback_text
    context.last_wait_metadata["runtime.last_clicked_callback_data_base64"] = callback_base64
