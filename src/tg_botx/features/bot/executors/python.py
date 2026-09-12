from __future__ import annotations

from typing import Any

from tg_botx.features.bot.executors.base import CommandContext, ExecutionError, ExecutionResult
from tg_botx.integrations.python_runner import PythonRunnerClient


class PythonExecutor:
    def __init__(self, runner: PythonRunnerClient | None):
        self.runner = runner

    async def execute(self, config: dict[str, Any], context: CommandContext) -> ExecutionResult:
        if self.runner is None:
            raise ExecutionError("EXECUTOR_UNAVAILABLE")
        return await self.runner.execute(config, context)

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.close()
