from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tg_botx.features.checkin.condition import (
    ConditionInput,
    select_branch,
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
    report.condition_reported = True
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
