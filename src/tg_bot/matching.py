from __future__ import annotations

import re
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


def match_button(message: Any, selector: dict[str, Any]) -> Any:
    buttons = getattr(message, "buttons", None) or []
    candidates = []
    wanted_text = selector.get("text")
    wanted_callback = selector.get("callback_data")
    wanted_row = selector.get("row")
    wanted_column = selector.get("column")

    for row_index, row in enumerate(buttons):
        for column_index, button in enumerate(row):
            if wanted_row is not None and row_index != wanted_row:
                continue
            if wanted_column is not None and column_index != wanted_column:
                continue
            button_text = str(getattr(button, "text", ""))
            callback = getattr(button, "data", None)
            callback_text = callback.decode() if isinstance(callback, bytes) else str(callback or "")
            if wanted_callback is not None and callback_text != str(wanted_callback):
                continue
            if wanted_callback is None and wanted_text is not None and button_text.casefold() != str(wanted_text).casefold():
                continue
            candidates.append(button)

    if len(candidates) != 1:
        raise ValueError(f"按钮匹配结果数量为 {len(candidates)}，要求唯一匹配")
    return candidates[0]
