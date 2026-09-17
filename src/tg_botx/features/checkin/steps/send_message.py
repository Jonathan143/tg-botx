from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tg_botx.features.checkin.condition import (
    render_template,
)
from tg_botx.features.checkin.execution_types import CheckinError, ExecutionContext, StepReport
from tg_botx.features.message_library.models import (
    MessageGroupConflict,
    MessageGroupNotFound,
    validate_message_text,
)

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
    if step.get("message_mode", "fixed") == "random":
        if step.get("random_source", "manual") == "group":
            if self.message_group_loader is None:
                raise CheckinError("当前执行器未配置消息库，无法读取实时分组")
            try:
                messages = await asyncio.to_thread(
                    self.message_group_loader, step["message_group_id"]
                )
            except (MessageGroupNotFound, MessageGroupConflict) as exc:
                raise CheckinError(str(exc)) from exc
        else:
            messages = step.get("messages", [])
        if not messages:
            raise CheckinError("随机消息候选列表为空")
        raw_text = secrets.choice(messages)
    else:
        raw_text = str(step["text"])
    text = render_template(raw_text, context.variables)
    try:
        validate_message_text(text)
    except ValueError as exc:
        raise CheckinError(str(exc)) from exc
    context.current_message = await self.client.send_message(context.entity, text)
    context.baseline = max(context.baseline, context.current_message.id)
    context.editable_message_ids.clear()
    context.editable_message_texts.clear()
