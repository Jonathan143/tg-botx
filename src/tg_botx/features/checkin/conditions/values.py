from __future__ import annotations

try:
    import regex as safe_regex
except ImportError:
    safe_regex = None

import base64
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from tg_botx.features.checkin.conditions.types import (
    NUMBER_CANDIDATE,
    REGEX_MATCH_TIMEOUT_SECONDS,
    ConditionEvaluationError,
    ConditionVariable,
    ValueType,
)


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
