from __future__ import annotations

import re
from typing import Any

from tg_botx.features.bot.executors.base import CommandContext, ExecutionError, json_bytes

_VARIABLE = re.compile(r"{{\s*([a-zA-Z_.]+)\s*}}")
_ALLOWED = {"argument", "command", "user.id", "user.role", "chat.id"}


def validate_templates(value: Any) -> None:
    if isinstance(value, str):
        names = _VARIABLE.findall(value)
        remaining = _VARIABLE.sub("", value)
        if any(name not in _ALLOWED for name in names) or "{{" in remaining or "}}" in remaining:
            raise ValueError("模板只能引用 argument、command、user.id、user.role、chat.id")
    elif isinstance(value, dict):
        for key, child in value.items():
            if "{{" in key or "}}" in key:
                raise ValueError("对象键不能包含模板变量")
            validate_templates(child)
    elif isinstance(value, list):
        for child in value:
            validate_templates(child)


def render(value: Any, context: CommandContext) -> Any:
    variables: dict[str, str | int | None] = {
        "argument": context.argument,
        "command": context.command,
        "user.id": context.user_id,
        "user.role": context.role,
        "chat.id": context.chat_id,
    }

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables or variables[name] is None:
            raise ExecutionError("MISSING_TEMPLATE_VARIABLE")
        return str(variables[name])

    def visit(item: Any) -> Any:
        if isinstance(item, str):
            return _VARIABLE.sub(replace, item)
        if isinstance(item, dict):
            return {key: visit(child) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    result = visit(value)
    json_bytes(result)
    return result
