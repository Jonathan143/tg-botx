from __future__ import annotations

import hashlib

from tg_botx.config import Settings
from tg_botx.features.bot.execution import CommandExecutionService
from tg_botx.features.bot.executors.builtin import BuiltinFunctionExecutor
from tg_botx.features.bot.executors.http import HttpExecutor
from tg_botx.features.bot.executors.policy import ExecutorPolicy
from tg_botx.features.bot.executors.python import PythonExecutor
from tg_botx.features.bot.executors.registry import ExecutorRegistry
from tg_botx.infrastructure.persistence.db import Database
from tg_botx.integrations.python_runner import PythonRunnerClient
from tg_botx.integrations.safe_http import SafeHttpClient


def build_command_execution(settings: Settings, database: Database) -> CommandExecutionService:
    policy = ExecutorPolicy(
        allowed_origins=frozenset(settings.command_http_allowed_origins),
        credentials=settings.command_http_credentials,
        python_enabled=settings.command_python_enabled,
        max_workers=settings.command_max_workers,
        python_workers=settings.command_python_workers,
        queue_limit=settings.command_queue_limit,
        queue_seconds=settings.command_queue_seconds,
        rate_limit=settings.command_rate_limit,
        retention_days=settings.command_retention_days,
    )
    registry = ExecutorRegistry(policy)
    runner = None
    if (
        settings.command_python_enabled
        and settings.command_python_runner_url
        and settings.command_python_runner_token
    ):
        runner = PythonRunnerClient(
            settings.command_python_runner_url,
            settings.command_python_runner_token.get_secret_value(),
        )
    registry.executors = {
        "http": HttpExecutor(SafeHttpClient(policy)),
        "builtin_function": BuiltinFunctionExecutor(database),
        "python": PythonExecutor(runner),
    }
    token = settings.admin_bot_token.get_secret_value() if settings.admin_bot_token else ""
    identity = (
        token.split(":", 1)[0] if ":" in token else hashlib.sha256(token.encode()).hexdigest()
    )
    return CommandExecutionService(database, registry, f"bot:{identity}", runner=runner)
