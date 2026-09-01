from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tg_botx.features.checkin.condition import (
    BETWEEN_OPERATORS,
    DATETIME_OPERATORS,
    LENGTH_OPERATORS,
    MAX_PATTERN_LENGTH,
    METADATA_FIELDS,
    NUMBER_OPERATORS,
    TEXT_OPERATORS,
    UNARY_OPERATORS,
    VARIABLE_NAME,
    template_names,
)

_MODEL_CONFIG = ConfigDict(extra="forbid")

_VALID_STEP_TYPES = {"send_message", "wait_message", "click_button", "condition"}
_STEP_FIELDS = {
    "send_message": {"type", "node_id", "text"},
    "wait_message": {"type", "node_id", "timeout_seconds", "success", "failure"},
    "click_button": {
        "type",
        "node_id",
        "text",
        "text_contains",
        "callback_data",
        "row",
        "column",
    },
    "condition": {
        "type",
        "node_id",
        "schema_version",
        "strict",
        "extracts",
        "branches",
    },
}
_LEGACY_CONDITION_FIELDS = {
    "type",
    "nodeId",
    "schemaVersion",
    "extract",
    "branches",
    "strict",
}
_EXTRACT_FIELDS = {
    "name",
    "source",
    "field",
    "mode",
    "value_type",
    "pattern",
    "capture_group",
    "regex",
}
_BRANCH_FIELDS = {"kind", "name", "logic", "conditions", "steps"}
_CONDITION_FIELDS = {
    "variable",
    "value_type",
    "operator",
    "operands",
    "normalization",
    "regex",
}
_OPERAND_FIELDS = {"source", "value", "name"}
_NORMALIZATION_FIELDS = {"trim", "ignore_case", "collapse_whitespace", "strip_markdown"}
_REGEX_FIELDS = {"ignore_case", "multiline", "match_mode"}


def _unsupported(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unsupported = set(value) - allowed
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise ValueError(f"{path} 包含不支持的字段: {names}")


def _validate_node_id(value: Any, path: str, node_ids: set[str]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ValueError(f"{path}.node_id 必须是 1-100 字符的字符串")
    if value in node_ids:
        raise ValueError(f"{path}.node_id 与其他节点重复")
    node_ids.add(value)


def _validate_regex_config(value: Any, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须是对象")
    _unsupported(value, _REGEX_FIELDS, path)
    for field in ("ignore_case", "multiline"):
        if field in value and not isinstance(value[field], bool):
            raise ValueError(f"{path}.{field} 必须是布尔值")
    if value.get("match_mode", "search") not in {"search", "full"}:
        raise ValueError(f"{path}.match_mode 必须是 search 或 full")


def _validate_template(value: Any, path: str, possible: dict[str, str]) -> None:
    if not isinstance(value, str):
        return
    unknown = template_names(value) - possible.keys()
    if unknown:
        raise ValueError(f"{path} 引用了未知变量: {', '.join(sorted(unknown))}")


def _validate_matcher_templates(value: Any, path: str, possible: dict[str, str]) -> None:
    if isinstance(value, str):
        _validate_template(value, path, possible)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_matcher_templates(item, f"{path}[{index}]", possible)
    elif isinstance(value, dict) and "value" in value:
        _validate_template(value["value"], f"{path}.value", possible)


def _validate_operand(
    operand: Any,
    path: str,
    expected_type: str,
    possible: dict[str, str],
) -> None:
    if not isinstance(operand, dict):
        raise ValueError(f"{path} 必须是对象")
    _unsupported(operand, _OPERAND_FIELDS, path)
    source = operand.get("source", "literal")
    if source == "literal":
        if "value" not in operand:
            raise ValueError(f"{path} 固定值必须配置 value")
    elif source == "variable":
        name = operand.get("name")
        if not isinstance(name, str) or name not in possible:
            raise ValueError(f"{path} 引用了未知变量")
        if possible[name] != expected_type:
            raise ValueError(f"{path} 变量 {name} 的类型必须是 {expected_type}")
    else:
        raise ValueError(f"{path}.source 必须是 literal 或 variable")


def _validate_rule(rule: Any, path: str, possible: dict[str, str]) -> None:
    if not isinstance(rule, dict):
        raise ValueError(f"{path} 必须是对象")
    _unsupported(rule, _CONDITION_FIELDS, path)
    name = rule.get("variable")
    if not isinstance(name, str) or name not in possible:
        raise ValueError(f"{path}.variable 引用了未知变量")
    value_type = rule.get("value_type")
    if value_type not in {"text", "number", "datetime"}:
        raise ValueError(f"{path}.value_type 无效")
    if possible[name] != value_type:
        raise ValueError(f"{path}.variable 与 value_type 类型不符")
    operator = rule.get("operator")
    allowed_operators = {
        "text": TEXT_OPERATORS,
        "number": NUMBER_OPERATORS | {"exists"},
        "datetime": DATETIME_OPERATORS,
    }[value_type]
    if operator not in allowed_operators:
        raise ValueError(f"{path}.operator 不支持 {value_type} 类型")
    operands = rule.get("operands", [])
    if not isinstance(operands, list):
        raise ValueError(f"{path}.operands 必须是数组")
    expected_count: int | None
    if operator in UNARY_OPERATORS:
        expected_count = 0
    elif operator in BETWEEN_OPERATORS:
        expected_count = 2
    elif operator == "in":
        expected_count = None
        if not operands:
            raise ValueError(f"{path}.operands 至少需要一项")
    else:
        expected_count = 1
    if expected_count is not None and len(operands) != expected_count:
        raise ValueError(f"{path}.operands 数量必须是 {expected_count}")
    operand_type = "number" if operator in LENGTH_OPERATORS else value_type
    for index, operand in enumerate(operands):
        _validate_operand(operand, f"{path}.operands[{index}]", operand_type, possible)
    normalization = rule.get("normalization")
    if normalization is not None:
        if not isinstance(normalization, dict):
            raise ValueError(f"{path}.normalization 必须是对象")
        _unsupported(normalization, _NORMALIZATION_FIELDS, f"{path}.normalization")
        if any(not isinstance(item, bool) for item in normalization.values()):
            raise ValueError(f"{path}.normalization 的开关必须是布尔值")
    regex_config = rule.get("regex")
    if operator == "regex":
        _validate_regex_config(regex_config or {}, f"{path}.regex")
        if operands and operands[0].get("source", "literal") == "literal":
            pattern = operands[0].get("value")
            if not isinstance(pattern, str) or not pattern:
                raise ValueError(f"{path} 正则固定值不能为空")
            if len(pattern) > MAX_PATTERN_LENGTH:
                raise ValueError(f"{path} 正则不能超过 {MAX_PATTERN_LENGTH} 个字符")
    elif regex_config is not None:
        raise ValueError(f"{path}.regex 只能用于正则运算符")


def _validate_extract(
    extraction: Any,
    path: str,
    declared: dict[str, str],
    possible: dict[str, str],
) -> None:
    if not isinstance(extraction, dict):
        raise ValueError(f"{path} 必须是对象")
    _unsupported(extraction, _EXTRACT_FIELDS, path)
    name = extraction.get("name")
    if not isinstance(name, str) or not VARIABLE_NAME.fullmatch(name) or name.startswith("__"):
        raise ValueError(f"{path}.name 必须是合法变量名，且不能使用 __ 前缀")
    if name in possible or name in declared:
        raise ValueError(f"{path}.name 与当前执行路径中的变量重复")
    source = extraction.get("source", "message_text")
    mode = extraction.get("mode", "whole_text")
    value_type = extraction.get("value_type", "text")
    if value_type not in {"text", "number", "datetime"}:
        raise ValueError(f"{path}.value_type 无效")
    if source == "metadata":
        field = extraction.get("field")
        if field not in METADATA_FIELDS:
            raise ValueError(f"{path}.field 不是支持的元数据字段")
        if mode != "metadata":
            raise ValueError(f"{path} 元数据提取的 mode 必须是 metadata")
    elif source == "message_text":
        if mode not in {"whole_text", "first_number", "regex_capture"}:
            raise ValueError(f"{path}.mode 无效")
        if "field" in extraction:
            raise ValueError(f"{path}.field 只能用于元数据提取")
    else:
        raise ValueError(f"{path}.source 必须是 message_text 或 metadata")
    if mode == "first_number" and value_type != "number":
        raise ValueError(f"{path} 首个数字提取的 value_type 必须是 number")
    if mode == "regex_capture":
        pattern = extraction.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{path}.pattern 不能为空")
        if len(pattern) > MAX_PATTERN_LENGTH:
            raise ValueError(f"{path}.pattern 不能超过 {MAX_PATTERN_LENGTH} 个字符")
        group = extraction.get("capture_group", 1)
        if not isinstance(group, (int, str)) or isinstance(group, bool):
            raise ValueError(f"{path}.capture_group 必须是编号或名称")
        if (isinstance(group, int) and group < 0) or (isinstance(group, str) and not group):
            raise ValueError(f"{path}.capture_group 必须是非负编号或非空名称")
        _validate_regex_config(extraction.get("regex", {}), f"{path}.regex")
    else:
        for field in ("pattern", "capture_group", "regex"):
            if field in extraction:
                raise ValueError(f"{path}.{field} 只能用于正则捕获")
    declared[name] = value_type


def _validate_legacy_condition(
    step: dict[str, Any],
    path: str,
    condition_depth: int,
    node_ids: set[str],
) -> None:
    _unsupported(step, _LEGACY_CONDITION_FIELDS, path)
    legacy_id = step.get("nodeId")
    if legacy_id is not None:
        _validate_node_id(legacy_id, path, node_ids)
    branches = step.get("branches")
    if not isinstance(branches, list) or not 2 <= len(branches) <= 20:
        raise ValueError(f"{path} 必须配置 2-20 个分支")
    if not isinstance(branches[0], dict) or branches[0].get("kind", "if") != "if":
        raise ValueError(f"{path} 第一个分支必须是 if")
    if not isinstance(branches[-1], dict) or branches[-1].get("kind") != "else":
        raise ValueError(f"{path} 最后一个分支必须是 else")
    for index, branch in enumerate(branches):
        if not isinstance(branch, dict):
            raise ValueError(f"{path}.branches[{index}] 必须是对象")
        kind = branch.get("kind", "else-if")
        if kind not in {"if", "else-if", "else"}:
            raise ValueError(f"{path}.branches[{index}].kind 无效")
        conditions = branch.get("when")
        if kind != "else" and not isinstance(conditions, (dict, list)):
            raise ValueError(f"{path}.branches[{index}] 必须配置 when")
        if isinstance(conditions, list) and not 1 <= len(conditions) <= 10:
            raise ValueError(f"{path}.branches[{index}] 条件数必须是 1-10")
        _validate_step_sequence(
            branch.get("steps"),
            f"{path}.branches[{index}].steps",
            condition_depth=condition_depth,
            definite={},
            possible={},
            has_wait=True,
            node_ids=node_ids,
        )


def _validate_v2_condition(
    step: dict[str, Any],
    path: str,
    condition_depth: int,
    definite: dict[str, str],
    possible: dict[str, str],
    has_wait: bool,
    node_ids: set[str],
) -> tuple[dict[str, str], dict[str, str], bool]:
    _unsupported(step, _STEP_FIELDS["condition"], path)
    if step.get("schema_version") != 2:
        raise ValueError(f"{path}.schema_version 必须是 2")
    if not isinstance(step.get("strict", False), bool):
        raise ValueError(f"{path}.strict 必须是布尔值")
    if not isinstance(step.get("node_id"), str):
        raise ValueError(f"{path}.node_id 必须配置")
    _validate_node_id(step.get("node_id"), path, node_ids)
    extracts = step.get("extracts", [])
    if not isinstance(extracts, list) or len(extracts) > 10:
        raise ValueError(f"{path}.extracts 必须是最多 10 项的数组")
    if extracts and not has_wait:
        raise ValueError(f"{path} 的所有到达路径都必须先成功等待消息")
    declared: dict[str, str] = {}
    for index, extraction in enumerate(extracts):
        _validate_extract(extraction, f"{path}.extracts[{index}]", declared, possible)
    condition_definite = {**definite, **declared}
    condition_possible = {**possible, **declared}
    branches = step.get("branches")
    if not isinstance(branches, list) or not 2 <= len(branches) <= 20:
        raise ValueError(f"{path}.branches 必须包含 2-20 个分支")
    branch_results: list[tuple[dict[str, str], dict[str, str], bool]] = []
    for index, branch in enumerate(branches):
        branch_path = f"{path}.branches[{index}]"
        if not isinstance(branch, dict):
            raise ValueError(f"{branch_path} 必须是对象")
        _unsupported(branch, _BRANCH_FIELDS, branch_path)
        kind = branch.get("kind")
        expected_kind = "if" if index == 0 else "else" if index == len(branches) - 1 else "else_if"
        if kind != expected_kind:
            raise ValueError(f"{branch_path}.kind 必须是 {expected_kind}")
        name = branch.get("name")
        if name is not None and (not isinstance(name, str) or len(name) > 80):
            raise ValueError(f"{branch_path}.name 不能超过 80 个字符")
        if kind == "else":
            if "conditions" in branch or "logic" in branch:
                raise ValueError(f"{branch_path} else 分支不能配置条件")
        else:
            if branch.get("logic", "and") not in {"and", "or"}:
                raise ValueError(f"{branch_path}.logic 必须是 and 或 or")
            conditions = branch.get("conditions")
            if not isinstance(conditions, list) or not 1 <= len(conditions) <= 10:
                raise ValueError(f"{branch_path}.conditions 必须包含 1-10 条条件")
            for rule_index, rule in enumerate(conditions):
                _validate_rule(rule, f"{branch_path}.conditions[{rule_index}]", condition_possible)
        branch_results.append(
            _validate_step_sequence(
                branch.get("steps"),
                f"{branch_path}.steps",
                condition_depth=condition_depth,
                definite=condition_definite.copy(),
                possible=condition_possible.copy(),
                has_wait=has_wait,
                node_ids=node_ids,
            )
        )
    merged_possible: dict[str, str] = {}
    for _, branch_possible, _ in branch_results:
        for name, value_type in branch_possible.items():
            previous = merged_possible.get(name)
            if previous is not None and previous != value_type:
                raise ValueError(f"{path} 的互斥分支将变量 {name} 定义成了不同类型")
            merged_possible[name] = value_type
    definite_names = set.intersection(*(set(item[0]) for item in branch_results))
    merged_definite = {name: branch_results[0][0][name] for name in definite_names}
    return merged_definite, merged_possible, all(item[2] for item in branch_results)


def _validate_step_sequence(
    items: Any,
    path: str,
    *,
    condition_depth: int,
    definite: dict[str, str],
    possible: dict[str, str],
    has_wait: bool,
    node_ids: set[str],
) -> tuple[dict[str, str], dict[str, str], bool]:
    if not isinstance(items, list):
        raise ValueError(f"{path} 必须是数组")
    for index, step in enumerate(items):
        step_path = f"{path}[{index}]"
        if not isinstance(step, dict):
            raise ValueError(f"{step_path} 必须是对象")
        kind = step.get("type")
        if kind not in _VALID_STEP_TYPES:
            raise ValueError(f"{step_path}.type 无效")
        if kind == "condition":
            next_depth = condition_depth + 1
            if next_depth > 3:
                raise ValueError(f"{step_path} 条件节点嵌套深度不能超过 3")
            if step.get("schema_version") == 2:
                definite, possible, has_wait = _validate_v2_condition(
                    step,
                    step_path,
                    next_depth,
                    definite,
                    possible,
                    has_wait,
                    node_ids,
                )
            else:
                _validate_legacy_condition(step, step_path, next_depth, node_ids)
            continue
        _unsupported(step, _STEP_FIELDS[kind], step_path)
        _validate_node_id(step.get("node_id"), step_path, node_ids)
        if kind == "send_message":
            if not isinstance(step.get("text"), str):
                raise ValueError(f"{step_path}.text 必须是字符串")
            _validate_template(step["text"], f"{step_path}.text", possible)
        elif kind == "wait_message":
            timeout = step.get("timeout_seconds", 60)
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
                raise ValueError(f"{step_path}.timeout_seconds 必须是正整数")
            _validate_matcher_templates(step.get("success"), f"{step_path}.success", possible)
            _validate_matcher_templates(step.get("failure"), f"{step_path}.failure", possible)
            has_wait = True
        elif kind == "click_button":
            text_selectors = [
                key for key in ("text", "text_contains", "callback_data") if key in step
            ]
            has_row = "row" in step
            has_column = "column" in step
            if has_row != has_column:
                raise ValueError(f"{step_path}.row 和 column 必须同时配置")
            selector_count = len(text_selectors) + int(has_row and has_column)
            if selector_count == 0:
                raise ValueError(f"{step_path} 至少需要一种按钮定位方式")
            if selector_count > 1:
                raise ValueError(f"{step_path} 按钮定位方式必须互斥")
            for field in ("text", "text_contains", "callback_data"):
                if field in step:
                    if not isinstance(step[field], str):
                        raise ValueError(f"{step_path}.{field} 必须是字符串")
                    _validate_template(step[field], f"{step_path}.{field}", possible)
            for field in ("row", "column"):
                if field in step and (
                    not isinstance(step[field], int)
                    or isinstance(step[field], bool)
                    or step[field] < 0
                ):
                    raise ValueError(f"{step_path}.{field} 必须是非负整数")
    return definite, possible, has_wait


class RetryConfig(BaseModel):
    model_config = _MODEL_CONFIG

    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=lambda: [30, 60, 120]
    )


class NotificationConfig(BaseModel):
    model_config = _MODEL_CONFIG

    failure: bool = True
    success: bool = False


class ScheduleConfig(BaseModel):
    model_config = _MODEL_CONFIG

    type: Literal["fixed", "random"]
    timezone: str = "Asia/Shanghai"
    frequency: Literal["daily", "every_n_days", "weekly", "monthly_dates"] = "daily"
    start_date: date | None = None
    end_date: date | None = None
    interval_days: int | None = Field(default=None, ge=1, le=365)
    weekdays: list[int] | None = None
    month_days: list[int] | None = None
    time: str | None = None
    start: str | None = None
    end: str | None = None

    @field_validator("time", "start", "end")
    @classmethod
    def valid_time(cls, value: str | None) -> str | None:
        if value is not None:
            time.fromisoformat(value)
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("调度 timezone 无效") from exc
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> ScheduleConfig:
        if self.type == "fixed" and not self.time:
            raise ValueError("fixed 调度必须配置 time")
        if self.type == "random" and (not self.start or not self.end):
            raise ValueError("random 调度必须配置 start 和 end")
        if self.type == "random":
            assert self.start is not None and self.end is not None
            if time.fromisoformat(self.end) <= time.fromisoformat(self.start):
                raise ValueError("随机时间窗口暂不支持跨午夜，end 必须晚于 start")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("schedule.end_date 不能早于 start_date")
        if self.frequency == "every_n_days":
            if self.interval_days is None:
                raise ValueError("every_n_days 调度必须配置 interval_days")
        elif self.interval_days is not None:
            raise ValueError("interval_days 只能用于 every_n_days 调度")
        if self.frequency == "weekly":
            if not self.weekdays:
                raise ValueError("weekly 调度至少选择一天")
            if any(isinstance(day, bool) or day < 1 or day > 7 for day in self.weekdays):
                raise ValueError("weekdays 必须是 1-7 的 ISO 星期编号")
            if len(set(self.weekdays)) != len(self.weekdays):
                raise ValueError("weekdays 不能包含重复值")
        elif self.weekdays is not None:
            raise ValueError("weekdays 只能用于 weekly 调度")
        if self.frequency == "monthly_dates":
            if not self.month_days:
                raise ValueError("monthly_dates 调度至少选择一个日期")
            if any(isinstance(day, bool) or day < 1 or day > 31 for day in self.month_days):
                raise ValueError("month_days 必须是 1-31 的日期")
            if len(set(self.month_days)) != len(self.month_days):
                raise ValueError("month_days 不能包含重复值")
        elif self.month_days is not None:
            raise ValueError("month_days 只能用于 monthly_dates 调度")
        return self


class TaskDefinition(BaseModel):
    model_config = _MODEL_CONFIG

    name: str = Field(min_length=1, max_length=150)
    account: str = "default"
    target: str = Field(min_length=1, max_length=200)
    schedule: ScheduleConfig
    retry: RetryConfig = Field(default_factory=RetryConfig)
    steps: list[dict[str, Any]] = Field(min_length=1)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    log_bot_response: bool | None = None
    log_condition_values: bool | None = None
    notify_bot_response: bool | None = None

    @model_validator(mode="after")
    def validate_steps(self) -> TaskDefinition:
        _validate_step_sequence(
            self.steps,
            "steps",
            condition_depth=0,
            definite={},
            possible={},
            has_wait=False,
            node_ids=set(),
        )
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> TaskDefinition:
        with path.open("r", encoding="utf-8") as file:
            return cls.model_validate(yaml.safe_load(file))

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False
        )

    def to_api_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
