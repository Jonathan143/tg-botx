from __future__ import annotations

import re
import unicodedata
from typing import Any


def matches(text: str, rule: Any) -> bool:
    if rule is None:
        return False
    if isinstance(rule, str):
        return rule.casefold() in text.casefold()
    if isinstance(rule, list):
        return any(matches(text, item) for item in rule)
    if not isinstance(rule, dict):
        return False
    mode = rule.get("mode", "contains")
    value = str(rule.get("value", ""))
    if mode == "exact":
        return text.casefold().strip() == value.casefold().strip()
    if mode == "regex":
        return re.search(value, text, flags=re.IGNORECASE) is not None
    return value.casefold() in text.casefold()


def _button_text(value: Any) -> str:
    """Normalize button labels before comparing them.

    Telegram labels can contain non-breaking spaces, zero-width formatting
    characters, or full-width punctuation.  Those characters are invisible
    in the workflow editor but would otherwise make a contains comparison
    fail unexpectedly.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(
        char for char in text if unicodedata.category(char) != "Cf" and not char.isspace()
    )
    return text.casefold()


def _callback_text(value: Any) -> str:
    if isinstance(value, bytes):
        # Callback data is an opaque Telegram payload.  It is common for it
        # to contain arbitrary bytes, so decoding must never break text-based
        # button matching.
        return value.decode("utf-8", errors="surrogateescape")
    return str(value or "")


def match_button(message: Any, selector: dict[str, Any]) -> Any:
    unsupported = set(selector) - {
        "type",
        "text",
        "text_contains",
        "callback_data",
        "row",
        "column",
    }
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise ValueError(f"按钮定位包含不支持的字段: {names}")

    buttons = getattr(message, "buttons", None) or []
    candidates = []
    wanted_text = selector.get("text")
    wanted_text_contains = selector.get("text_contains")
    wanted_callback = selector.get("callback_data")
    wanted_row = selector.get("row")
    wanted_column = selector.get("column")

    for row_index, row in enumerate(buttons):
        for column_index, button in enumerate(row):
            if wanted_row is not None and row_index != wanted_row:
                continue
            if wanted_column is not None and column_index != wanted_column:
                continue
            if wanted_callback is not None:
                callback = getattr(button, "data", None)
                if _callback_text(callback) != _callback_text(wanted_callback):
                    continue
            candidates.append(button)

    if wanted_callback is None:
        if wanted_text_contains is not None:
            needle = _button_text(wanted_text_contains)
            candidates = [
                button
                for button in candidates
                if needle in _button_text(getattr(button, "text", ""))
            ]
        elif wanted_text is not None:
            # Preserve exact matching when possible, then allow a unique
            # substring match such as "每日签到" -> "✅每日签到".
            needle = _button_text(wanted_text)
            exact = [
                button
                for button in candidates
                if _button_text(getattr(button, "text", "")) == needle
            ]
            candidates = exact or [
                button
                for button in candidates
                if needle in _button_text(getattr(button, "text", ""))
            ]

    if len(candidates) != 1:
        raise ValueError(f"按钮匹配结果数量为 {len(candidates)}，要求唯一匹配")
    return candidates[0]
