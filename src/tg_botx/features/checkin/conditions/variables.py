from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo

from tg_botx.features.checkin.conditions.regex import execute_regex
from tg_botx.features.checkin.conditions.types import (
    TEMPLATE_TOKEN,
    ConditionEvaluationError,
    ConditionInput,
    ConditionVariable,
    RegexBudget,
    ValueType,
)
from tg_botx.features.checkin.conditions.values import convert_value, first_number


def template_names(value: str) -> set[str]:
    return {match.group(1) for match in TEMPLATE_TOKEN.finditer(value)}


def render_template(value: str, variables: dict[str, ConditionVariable]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        variable = variables.get(name)
        if variable is None:
            raise ConditionEvaluationError(f"模板变量不存在：{name}")
        return variable.raw

    return TEMPLATE_TOKEN.sub(replace, value).replace(r"\{{", "{{")


def render_matcher_templates(value: Any, variables: dict[str, ConditionVariable]) -> Any:
    if isinstance(value, str):
        return render_template(value, variables)
    if isinstance(value, list):
        return [render_matcher_templates(item, variables) for item in value]
    if isinstance(value, dict):
        return {
            key: render_template(item, variables)
            if key == "value" and isinstance(item, str)
            else item
            for key, item in value.items()
        }
    return value


def extract_variables(
    step: dict[str, Any],
    condition_input: ConditionInput,
    variables: dict[str, ConditionVariable],
    budget: RegexBudget,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    strict = bool(step.get("strict", False))
    for extraction in step.get("extracts", []):
        name = str(extraction.get("name", ""))
        try:
            source = extraction.get("source", "message_text")
            if source == "metadata":
                field = str(extraction.get("field", ""))
                if field not in condition_input.metadata or condition_input.metadata[field] is None:
                    raise ConditionEvaluationError(f"元数据不存在：{field}")
                raw: Any = condition_input.metadata[field]
            else:
                if condition_input.message_text is None:
                    raise ConditionEvaluationError("条件节点前没有可用的等待消息")
                raw = condition_input.message_text
            mode = extraction.get("mode", "whole_text")
            if mode == "first_number":
                raw = first_number(raw)
            elif mode == "regex_capture":
                raw = execute_regex(
                    str(extraction.get("pattern", "")),
                    str(raw),
                    extraction.get("regex") or {},
                    budget,
                    capture_group=extraction.get("capture_group", 1),
                )
            value_type = extraction.get("value_type", "text")
            variable = convert_value(name, raw, value_type, condition_input.timezone)
            variables[name] = variable
            results.append(
                {
                    "name": name,
                    "valueType": value_type,
                    "status": "success",
                    "value": variable.raw,
                }
            )
        except ConditionEvaluationError as exc:
            variables.pop(name, None)
            results.append(
                {
                    "name": name,
                    "valueType": extraction.get("value_type", "text"),
                    "status": "failed",
                    "error": str(exc),
                }
            )
            if strict:
                raise
    return results


def _resolve_operand(
    operand: dict[str, Any],
    variables: dict[str, ConditionVariable],
    value_type: ValueType,
    timezone: ZoneInfo,
) -> ConditionVariable:
    if operand.get("source", "literal") == "variable":
        name = str(operand.get("name", ""))
        variable = variables.get(name)
        if variable is None:
            raise ConditionEvaluationError(f"变量不存在：{name}")
        if variable.value_type != value_type:
            raise ConditionEvaluationError(
                f"变量 {name} 类型为 {variable.value_type}，不能作为 {value_type} 操作数"
            )
        return variable
    return convert_value("__literal", operand.get("value", ""), value_type, timezone)
