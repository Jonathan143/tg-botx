from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tg_botx.features.checkin.condition import (
    render_template,
)
from tg_botx.features.checkin.execution_types import ExecutionContext, StepReport

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
    text = render_template(str(step["text"]), context.variables)
    context.current_message = await self.client.send_message(context.entity, text)
    context.baseline = max(context.baseline, context.current_message.id)
    context.editable_message_ids.clear()
    context.editable_message_texts.clear()
