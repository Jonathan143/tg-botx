from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

try:  # The dependency is required in production; keeping import optional lets non-condition tools load.
    import regex as safe_regex
except ImportError:  # pragma: no cover - exercised only in incomplete local environments
    safe_regex = None


ValueType = Literal["text", "number", "datetime"]

VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
TEMPLATE_TOKEN = re.compile(r"(?<!\\)\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}")
NUMBER_CANDIDATE = re.compile(r"[-+]?\d(?:[\d, \u00a0\u202f]*\d)?(?:\.\d+)?")

MAX_PATTERN_LENGTH = 500
MAX_REGEX_INPUT_LENGTH = 16_384
REGEX_MATCH_TIMEOUT_SECONDS = 0.05
REGEX_NODE_BUDGET_SECONDS = 0.2

METADATA_FIELDS: dict[str, ValueType] = {
    "sender.id": "number",
    "sender.username": "text",
    "sender.display_name": "text",
    "chat.id": "number",
    "chat.title": "text",
    "chat.username": "text",
    "chat.type": "text",
    "message.id": "number",
    "message.date": "datetime",
    "message.text": "text",
    "message.type": "text",
    "runtime.last_clicked_callback_data_text": "text",
    "runtime.last_clicked_callback_data_base64": "text",
}

NUMBER_OPERATORS = {"gt", "gte", "lt", "lte", "eq", "ne", "between", "in"}
DATETIME_OPERATORS = {
    "before",
    "before_or_equal",
    "after",
    "after_or_equal",
    "eq",
    "ne",
    "between",
    "in",
    "exists",
}
TEXT_OPERATORS = {
    "exact",
    "not_exact",
    "contains",
    "regex",
    "starts_with",
    "ends_with",
    "length_eq",
    "length_ne",
    "length_gt",
    "length_gte",
    "length_lt",
    "length_lte",
    "length_between",
    "in",
    "exists",
    "empty",
    "not_empty",
}
UNARY_OPERATORS = {"exists", "empty", "not_empty"}
BETWEEN_OPERATORS = {"between", "length_between"}
LENGTH_OPERATORS = {
    "length_eq",
    "length_ne",
    "length_gt",
    "length_gte",
    "length_lt",
    "length_lte",
    "length_between",
}


class ConditionEvaluationError(ValueError):
    """A recoverable condition error that strict mode can promote to task failure."""


@dataclass(frozen=True, slots=True)
class ConditionVariable:
    name: str
    value_type: ValueType
    raw: str
    value: str | Decimal | datetime


@dataclass(slots=True)
class RegexBudget:
    remaining: float = REGEX_NODE_BUDGET_SECONDS

    def spend(self, elapsed: float) -> None:
        self.remaining = max(0.0, self.remaining - elapsed)


@dataclass(frozen=True, slots=True)
class ConditionInput:
    message_text: str | None
    metadata: dict[str, Any]
    timezone: ZoneInfo


def callback_data_values(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, str):
        raw = value.encode()
        text = value
    else:
        try:
            raw = bytes(value)
        except (TypeError, ValueError):
            return None, None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
    return text, base64.b64encode(raw).decode("ascii")


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


def parse_number(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ConditionEvaluationError("布尔值不能作为数值")
    raw = str(value).strip()
    if not raw:
        raise ConditionEvaluationError("数值为空")
    sign = ""
    if raw[0] in "+-":
        sign, raw = raw[0], raw[1:]
    if raw.count(".") > 1:
        raise ConditionEvaluationError("数值包含多个小数点")
    integer, separator, fraction = raw.partition(".")
    if separator and (not fraction or not fraction.isdigit()):
        raise ConditionEvaluationError("数值的小数部分无效")
    comma = "," in integer
    whitespace_chars = {character for character in integer if character in " \u00a0\u202f"}
    if comma and whitespace_chars:
        raise ConditionEvaluationError("数值不能混用逗号和空格千分位")
    if comma:
        groups = integer.split(",")
    elif whitespace_chars:
        normalized_spaces = re.sub(r"[ \u00a0\u202f]", " ", integer)
        groups = normalized_spaces.split(" ")
    else:
        groups = [integer]
    if len(groups) > 1 and (
        not groups[0]
        or len(groups[0]) > 3
        or not groups[0].isdigit()
        or any(len(group) != 3 or not group.isdigit() for group in groups[1:])
    ):
        raise ConditionEvaluationError("数值的千分位分组无效")
    if len(groups) == 1 and (not integer or not integer.isdigit()):
        raise ConditionEvaluationError("数值格式无效")
    normalized = f"{sign}{''.join(groups)}"
    if separator:
        normalized += f".{fraction}"
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ConditionEvaluationError("数值格式无效") from exc


def first_number(value: Any) -> str:
    text = str(value)
    match = NUMBER_CANDIDATE.search(text)
    if match is None:
        raise ConditionEvaluationError("未找到数字")
    candidate = match.group(0)
    parse_number(candidate)
    return candidate


def _attach_timezone(value: datetime, timezone: ZoneInfo) -> datetime:
    first = value.replace(tzinfo=timezone, fold=0)
    second = value.replace(tzinfo=timezone, fold=1)
    first_valid = first.astimezone(UTC).astimezone(timezone).replace(tzinfo=None) == value
    second_valid = second.astimezone(UTC).astimezone(timezone).replace(tzinfo=None) == value
    if not first_valid and not second_valid:
        raise ConditionEvaluationError("日期时间落在任务时区不存在的夏令时时刻")
    if first_valid and second_valid and first.utcoffset() != second.utcoffset():
        raise ConditionEvaluationError("日期时间在任务时区存在夏令时歧义，请提供时区偏移")
    return first if first_valid else second


def parse_datetime(value: Any, timezone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        raw = str(value).strip()
        if not raw:
            raise ConditionEvaluationError("日期时间为空")
        if re.fullmatch(r"[-+]?\d{10}|[-+]?\d{13}", raw):
            timestamp = float(raw)
            if len(raw.lstrip("+-")) == 13:
                timestamp /= 1000
            try:
                return datetime.fromtimestamp(timestamp, tz=UTC).astimezone(timezone)
            except (OverflowError, OSError, ValueError) as exc:
                raise ConditionEvaluationError("Unix 时间戳超出范围") from exc
        normalized = raw.replace("Z", "+00:00")
        if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?", normalized):
            normalized = normalized.replace("/", "-")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ConditionEvaluationError("日期时间格式无效") from exc
    if parsed.tzinfo is None:
        parsed = _attach_timezone(parsed, timezone)
    return parsed.astimezone(timezone)


def strip_markdown(value: str) -> str:
    # Preserve rendered text while removing the Telegram Markdown/MarkdownV2 structure.
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"(^|\n)\s*(?:>|#{1,6})\s?", r"\1", value)
    value = re.sub(r"(?<!\\)(?:\*\*|__|~~|\|\||`{1,3})", "", value)
    return re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!])", r"\1", value)


def normalize_text(value: Any, rule: dict[str, Any]) -> str:
    text = str(value)
    if rule.get("strip_markdown", False):
        text = strip_markdown(text)
    if rule.get("collapse_whitespace", False):
        text = re.sub(r"\s+", " ", text)
    if rule.get("trim", True):
        text = text.strip()
    if rule.get("ignore_case", False):
        text = text.casefold()
    return text


def grapheme_length(value: str) -> int:
    if safe_regex is None:  # pragma: no cover - dependency is installed in production
        raise ConditionEvaluationError("条件正则依赖 regex 未安装")
    return len(safe_regex.findall(r"\X", value, timeout=REGEX_MATCH_TIMEOUT_SECONDS))


def _regex_flags(config: dict[str, Any]) -> int:
    flags = 0
    if config.get("ignore_case", False):
        flags |= safe_regex.IGNORECASE
    if config.get("multiline", False):
        flags |= safe_regex.MULTILINE
    return flags


def execute_regex(
    pattern: str,
    value: str,
    config: dict[str, Any],
    budget: RegexBudget,
    *,
    capture_group: int | str | None = None,
) -> str | bool:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ConditionEvaluationError(f"正则表达式不能超过 {MAX_PATTERN_LENGTH} 个字符")
    if len(value) > MAX_REGEX_INPUT_LENGTH:
        raise ConditionEvaluationError(f"正则输入不能超过 {MAX_REGEX_INPUT_LENGTH} 个字符")
    if budget.remaining <= 0:
        raise ConditionEvaluationError("条件节点正则执行预算已耗尽")
    if safe_regex is None:  # pragma: no cover - dependency is installed in production
        raise ConditionEvaluationError("条件正则依赖 regex 未安装")
    timeout = min(REGEX_MATCH_TIMEOUT_SECONDS, budget.remaining)
    started = time.perf_counter()
    try:
        compiled = safe_regex.compile(pattern, _regex_flags(config))
        if config.get("match_mode", "search") == "full":
            match = compiled.fullmatch(value, timeout=timeout)
        else:
            match = compiled.search(value, timeout=timeout)
    except TimeoutError as exc:
        raise ConditionEvaluationError("正则执行超时") from exc
    except safe_regex.error as exc:
        raise ConditionEvaluationError(f"正则表达式无效：{exc}") from exc
    finally:
        budget.spend(time.perf_counter() - started)
    if capture_group is None:
        return match is not None
    if match is None:
        raise ConditionEvaluationError("正则未匹配")
    try:
        captured = match.group(capture_group)
    except (IndexError, KeyError) as exc:
        raise ConditionEvaluationError(f"正则捕获组不存在：{capture_group}") from exc
    if captured is None:
        raise ConditionEvaluationError(f"正则捕获组未参与匹配：{capture_group}")
    return str(captured)


def _raw_string(value: Any, timezone: ZoneInfo) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = _attach_timezone(value, timezone)
        return value.astimezone(timezone).isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def convert_value(
    name: str, raw: Any, value_type: ValueType, timezone: ZoneInfo
) -> ConditionVariable:
    raw_string = _raw_string(raw, timezone)
    if value_type == "number":
        converted: str | Decimal | datetime = parse_number(raw)
    elif value_type == "datetime":
        converted = parse_datetime(raw, timezone)
    else:
        converted = raw_string
    return ConditionVariable(name=name, value_type=value_type, raw=raw_string, value=converted)


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
                    extraction.get("regex", {}),
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
