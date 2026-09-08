from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from tg_botx.features.checkin.condition import (
    ConditionInput,
    RegexBudget,
    ValueType,
    convert_value,
    extract_variables,
)
from tg_botx.features.checkin.execution_types import CheckinError, ExecutionContext, StepReport

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
    source_id = str(step.get("source_node_id") or "")
    source = step.get("source", "http_body")
    if source == "wait_message_text":
        if source_id and source_id not in context.wait_messages:
            raise CheckinError(f"等待消息数据源节点未执行或不存在：{source_id}")
        raw = context.wait_messages.get(source_id) if source_id else context.last_wait_text
        if raw is None and step.get("mode", "whole_text") != "metadata":
            raise CheckinError("等待消息前没有可提取的内容")
        metadata = (
            context.wait_metadata.get(source_id, {}) if source_id else context.last_wait_metadata
        )
        extraction = {
            "name": step["name"],
            "source": "metadata"
            if step.get("mode") == "metadata"
            else step.get("extract_source", "message_text"),
            "mode": step.get("mode", "whole_text"),
            "value_type": step.get("value_type", "text"),
            "field": step.get("field"),
            "pattern": step.get("pattern"),
            "capture_group": step.get("capture_group", 1),
            "regex": step.get("regex") or {},
        }
        extract_variables(
            {"extracts": [extraction], "strict": True},
            ConditionInput(
                message_text=raw,
                metadata=metadata,
                timezone=context.timezone,
            ),
            context.variables,
            RegexBudget(),
        )
    else:
        source_response = (
            context.http_responses.get(source_id) if source_id else context.http_response
        )
        if source_response is None:
            raise CheckinError(
                f"HTTP 数据源节点未执行或不存在：{source_id}"
                if source_id
                else "变量提取节点前没有 HTTP 响应"
            )
        if source == "http_status":
            raw = str(source_response.status_code)
        elif source == "http_headers":
            raw = json.dumps(dict(source_response.headers), ensure_ascii=False)
        else:
            raw = source_response.text
        path = step.get("path")
        if path and source == "http_body":
            try:
                value: Any = source_response.json()
                for part in str(path).lstrip("$.").split("."):
                    value = value[int(part)] if isinstance(value, list) else value[part]
                raw = str(value)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise CheckinError(f"无法提取响应字段：{path}") from exc
        context.variables[str(step["name"])] = convert_value(
            str(step["name"]),
            raw,
            cast(ValueType, step.get("value_type", "text")),
            context.timezone,
        )
