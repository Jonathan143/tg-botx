from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

ValueType = Literal["text", "number", "datetime"]

VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")

VARIABLE_RESERVED_WORDS = frozenset(
    {
        "False",
        "None",
        "True",
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "def",
        "default",
        "delete",
        "del",
        "do",
        "else",
        "elif",
        "enum",
        "except",
        "export",
        "extends",
        "finally",
        "for",
        "from",
        "function",
        "global",
        "if",
        "implements",
        "import",
        "in",
        "instanceof",
        "interface",
        "is",
        "lambda",
        "let",
        "new",
        "nonlocal",
        "not",
        "null",
        "or",
        "package",
        "pass",
        "private",
        "protected",
        "public",
        "raise",
        "return",
        "static",
        "super",
        "switch",
        "this",
        "throw",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "with",
        "yield",
    }
)

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
