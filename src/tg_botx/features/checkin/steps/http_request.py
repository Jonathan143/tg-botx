from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tg_botx.features.checkin.condition import (
    render_template,
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
    url = render_template(str(step["url"]), context.variables)
    try:
        headers = json.loads(step.get("headers") or "{}")
    except json.JSONDecodeError as exc:
        raise CheckinError("HTTP 请求头必须是有效 JSON") from exc
    if not isinstance(headers, dict):
        raise CheckinError("HTTP 请求头必须是 JSON 对象")
    if any(not isinstance(value, str) for value in headers.values()):
        raise CheckinError("HTTP 请求头的值必须是字符串")
    headers = {key: render_template(value, context.variables) for key, value in headers.items()}
    body = step.get("body")
    rendered_body = render_template(body, context.variables) if isinstance(body, str) else None
    kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": step.get("timeout_seconds", 30),
    }
    if rendered_body:
        try:
            kwargs["json"] = json.loads(rendered_body)
        except json.JSONDecodeError:
            kwargs["content"] = rendered_body
    client = self.http_client()
    response = await client.request(str(step.get("method", "GET")), url, **kwargs)
    response.raise_for_status()
    context.http_response = response
    context.http_responses[node_id or resolved_path] = response
    context.bot_response = response.text[:4000]
    await self.report_step_response(
        index, context.bot_response, node_id=node_id, step_path=resolved_path
    )
    report.response_reported = True
