from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from tg_botx.features.checkin.conditions.regex import execute_regex
from tg_botx.features.checkin.conditions.types import (
    LENGTH_OPERATORS,
    NUMBER_OPERATORS,
    ConditionEvaluationError,
    ConditionInput,
    ConditionVariable,
    RegexBudget,
    ValueType,
)
from tg_botx.features.checkin.conditions.values import grapheme_length, normalize_text, parse_number
from tg_botx.features.checkin.conditions.variables import _resolve_operand, extract_variables


def _compare_ordered(value: Any, expected: Any, operator: str) -> bool:
    if operator in {"eq", "exact", "length_eq"}:
        return value == expected
    if operator in {"ne", "not_exact", "length_ne"}:
        return value != expected
    if operator in {"gt", "after", "length_gt"}:
        return value > expected
    if operator in {"gte", "after_or_equal", "length_gte"}:
        return value >= expected
    if operator in {"lt", "before", "length_lt"}:
        return value < expected
    if operator in {"lte", "before_or_equal", "length_lte"}:
        return value <= expected
    raise ConditionEvaluationError(f"不支持的比较运算符：{operator}")


def evaluate_rule(
    rule: dict[str, Any],
    variables: dict[str, ConditionVariable],
    timezone: ZoneInfo,
    budget: RegexBudget,
) -> bool:
    variable_name = str(rule.get("variable", ""))
    variable = variables.get(variable_name)
    if variable is None:
        raise ConditionEvaluationError(f"变量不存在：{variable_name}")
    value_type: ValueType = rule.get("value_type", variable.value_type)
    if variable.value_type != value_type:
        raise ConditionEvaluationError(
            f"变量 {variable_name} 类型为 {variable.value_type}，不能按 {value_type} 判断"
        )
    operator = str(rule.get("operator", ""))
    operands = rule.get("operands", [])
    if operator == "exists":
        return True
    if value_type == "text":
        normalization = rule.get("normalization", {})
        value = normalize_text(variable.value, normalization)
        if operator == "empty":
            return not value
        if operator == "not_empty":
            return bool(value)
        if operator in LENGTH_OPERATORS:
            length = grapheme_length(value)
            expected_values = [
                parse_number(_resolve_operand(item, variables, "number", timezone).value)
                for item in operands
            ]
            if any(item != item.to_integral_value() or item < 0 for item in expected_values):
                raise ConditionEvaluationError("字符长度必须与非负整数比较")
            integers = [int(item) for item in expected_values]
            if operator == "length_between":
                return integers[0] <= length <= integers[1]
            return _compare_ordered(length, integers[0], operator)
        expected_variables = [
            _resolve_operand(item, variables, "text", timezone) for item in operands
        ]
        expected = [normalize_text(item.value, normalization) for item in expected_variables]
        if operator == "contains":
            return expected[0] in value
        if operator == "starts_with":
            return value.startswith(expected[0])
        if operator == "ends_with":
            return value.endswith(expected[0])
        if operator == "regex":
            raw_pattern = str(expected_variables[0].value)
            return bool(execute_regex(raw_pattern, value, rule.get("regex", {}), budget))
        if operator == "in":
            return value in expected
        return _compare_ordered(value, expected[0], operator)
    if value_type == "number":
        number_value = cast(Decimal, variable.value)
        expected_numbers = [
            cast(Decimal, _resolve_operand(item, variables, "number", timezone).value)
            for item in operands
        ]
        if operator == "between":
            return expected_numbers[0] <= number_value <= expected_numbers[1]
        if operator == "in":
            return number_value in expected_numbers
        return _compare_ordered(number_value, expected_numbers[0], operator)
    datetime_value = cast(datetime, variable.value)
    expected_datetimes = [
        cast(datetime, _resolve_operand(item, variables, "datetime", timezone).value)
        for item in operands
    ]
    if operator == "between":
        return expected_datetimes[0] <= datetime_value <= expected_datetimes[1]
    if operator == "in":
        return datetime_value in expected_datetimes
    return _compare_ordered(datetime_value, expected_datetimes[0], operator)


def select_branch(
    step: dict[str, Any],
    condition_input: ConditionInput,
    variables: dict[str, ConditionVariable],
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    budget = RegexBudget()
    extraction_results = extract_variables(step, condition_input, variables, budget)
    strict = bool(step.get("strict", False))
    branches = step.get("branches", [])
    for index, branch in enumerate(branches):
        if branch.get("kind") == "else":
            return index, branch, extraction_results
        logic = branch.get("logic", "and")
        conditions = branch.get("conditions", [])
        matched = logic == "and"
        for rule in conditions:
            try:
                result = evaluate_rule(rule, variables, condition_input.timezone, budget)
            except ConditionEvaluationError:
                if strict:
                    raise
                result = False
            if logic == "and" and not result:
                matched = False
                break
            if logic == "or" and result:
                matched = True
                break
            if logic == "or":
                matched = False
        if matched:
            return index, branch, extraction_results
    raise ConditionEvaluationError("条件节点缺少 else 分支")


def normalize_legacy_condition(step: dict[str, Any]) -> dict[str, Any]:
    if step.get("schema_version") == 2:
        return step
    extracts: list[dict[str, Any]] = []
    extraction = step.get("extract")
    if isinstance(extraction, dict) and extraction.get("name"):
        mode = extraction.get("mode", "whole_text")
        item: dict[str, Any] = {
            "name": extraction["name"],
            "source": "message_text",
            "mode": mode,
            "value_type": "number" if mode == "first_number" else "text",
        }
        if mode == "regex_capture":
            item.update(
                {
                    "pattern": str(extraction.get("pattern", "")),
                    "capture_group": extraction.get("group", 1),
                    "regex": {
                        "ignore_case": False,
                        "multiline": False,
                        "match_mode": "search",
                    },
                }
            )
        extracts.append(item)
    branches: list[dict[str, Any]] = []
    for branch_index, branch in enumerate(step.get("branches", [])):
        kind = str(branch.get("kind", "else-if")).replace("-", "_")
        normalized: dict[str, Any] = {
            "kind": kind,
            "name": branch.get("name") or branch.get("label"),
            "steps": branch.get("steps") or [],
        }
        if kind != "else":
            rules = branch.get("when") or {}
            rules = rules if isinstance(rules, list) else [rules]
            conditions: list[dict[str, Any]] = []
            for rule_index, legacy in enumerate(rules):
                operator = str(legacy.get("operator", "exact"))
                value_type: ValueType = "number" if operator in NUMBER_OPERATORS else "text"
                name = str(legacy.get("name", ""))
                source = legacy.get("source", "variable")
                if source != "variable":
                    name = f"__legacy_{branch_index}_{rule_index}"
                    mode = (legacy.get("extract") or {}).get("mode", "whole_text")
                    legacy_extract: dict[str, Any] = {
                        "name": name,
                        "source": "metadata" if source == "metadata" else "message_text",
                        "mode": "metadata" if source == "metadata" else mode,
                        "value_type": value_type,
                    }
                    if source == "metadata":
                        legacy_extract["field"] = legacy.get("field")
                    extracts.append(legacy_extract)
                raw_value = legacy.get("value")
                raw_values = raw_value if operator in {"between", "in"} else [raw_value]
                conditions.append(
                    {
                        "variable": name,
                        "value_type": value_type,
                        "operator": operator,
                        "operands": [
                            {"source": "literal", "value": value} for value in (raw_values or [])
                        ],
                        "normalization": {
                            "trim": bool(legacy.get("trim", False)),
                            "ignore_case": bool(legacy.get("ignore_case", False)),
                            "collapse_whitespace": False,
                            "strip_markdown": False,
                        },
                        "regex": {
                            "ignore_case": False,
                            "multiline": False,
                            "match_mode": "search",
                        },
                    }
                )
            normalized.update(
                {
                    "logic": str(branch.get("logic", "and")).lower(),
                    "conditions": conditions,
                }
            )
        branches.append(normalized)
    return {
        "type": "condition",
        "node_id": step.get("node_id") or step.get("nodeId"),
        "schema_version": 2,
        "strict": bool(step.get("strict", False)),
        "extracts": extracts,
        "branches": branches,
    }
