from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from tg_botx.core.time import utc_isoformat, utc_now
from tg_botx.features.bot.executors.base import CommandContext, ExecutionError, ExecutionResult
from tg_botx.features.bot.executors.schemas import BuiltinConfig, EchoArguments, EmptyArguments
from tg_botx.features.bot.executors.templates import render, validate_templates
from tg_botx.infrastructure.persistence.db import Database


@dataclass(frozen=True, slots=True)
class BuiltinSpec:
    description: str
    roles: tuple[str, ...]
    arguments: type[BaseModel]


BUILTINS: dict[str, BuiltinSpec] = {
    "echo": BuiltinSpec("返回指定文本", ("anonymous", "user", "admin"), EchoArguments),
    "utc_time": BuiltinSpec("返回 UTC 时间", ("anonymous", "user", "admin"), EmptyArguments),
    "my_points": BuiltinSpec("查询调用者本人的积分", ("user", "admin"), EmptyArguments),
    "system_status": BuiltinSpec("查询任务数量等非敏感状态", ("admin",), EmptyArguments),
}


def validate_builtin(config: BuiltinConfig) -> None:
    BUILTINS[config.function].arguments.model_validate(config.arguments)
    validate_templates(config.arguments)


def builtin_catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec.description,
            "allowedRoles": list(spec.roles),
            "hasSideEffects": False,
            "argumentsSchema": spec.arguments.model_json_schema(),
        }
        for name, spec in BUILTINS.items()
    ]


class BuiltinFunctionExecutor:
    def __init__(self, database: Database):
        self.database = database

    async def execute(self, config: dict[str, Any], context: CommandContext) -> ExecutionResult:
        parsed = BuiltinConfig.model_validate(config)
        spec = BUILTINS[parsed.function]
        if context.role not in spec.roles:
            raise ExecutionError("EXECUTION_FORBIDDEN")
        arguments = spec.arguments.model_validate(render(parsed.arguments, context))
        if parsed.function == "echo":
            assert isinstance(arguments, EchoArguments)
            result = ExecutionResult(arguments.text)
        elif parsed.function == "utc_time":
            result = ExecutionResult(utc_isoformat(utc_now()) or "")
        elif parsed.function == "my_points":
            if context.user_id is None or context.chat_id is None:
                raise ExecutionError("EXECUTION_FORBIDDEN")
            binding = self.database.get_bot_binding(context.user_id)
            if binding is None or binding.chat_id != context.chat_id:
                raise ExecutionError("EXECUTION_FORBIDDEN")
            points = self.database.get_bot_user_points(context.user_id)
            count = points.points if points else 0
            result = ExecutionResult(f"当前积分：{count}", {"points": count})
        else:
            tasks = self.database.list_tasks()
            data = {"tasks": len(tasks), "enabledTasks": sum(bool(task.enabled) for task in tasks)}
            result = ExecutionResult(
                f"任务总数：{data['tasks']}\n启用任务：{data['enabledTasks']}", data
            )
        return result.validated()

    async def close(self) -> None:
        pass
