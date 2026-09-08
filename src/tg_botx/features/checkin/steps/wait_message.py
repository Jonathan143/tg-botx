from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tg_botx.features.checkin.condition import (
    render_matcher_templates,
)
from tg_botx.features.checkin.execution_types import CheckinError as CheckinError
from tg_botx.features.checkin.execution_types import ExecutionContext as ExecutionContext
from tg_botx.features.checkin.execution_types import StepReport

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
    context.current_message = await self._hydrate_message(context.entity, context.current_message)
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
    context.wait_messages[node_id or resolved_path] = context.last_wait_text
    context.wait_metadata[node_id or resolved_path] = dict(context.last_wait_metadata)
    context.bot_response = context.last_wait_text
    context.bot_buttons = await self._message_buttons(context.current_message, context.entity)
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
    report.response_reported = True
    context.baseline = max(context.baseline, context.current_message.id)
    context.editable_message_ids.clear()
    context.editable_message_texts.clear()
