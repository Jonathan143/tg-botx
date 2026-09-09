from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from tg_botx.features.bot.commands import BotCommandService
from tg_botx.features.bot.models import BotCommandValidationError
from tg_botx.features.bot.executors import (
    CustomCommandConfigError,
    CustomCommandContext,
    CustomCommandExecutorError,
    execute_custom_command,
    validate_executor_config,
)


def context(argument: str = "Tokyo") -> CustomCommandContext:
    return CustomCommandContext("weather", argument, f"/weather {argument}", 100, 200)


@pytest.mark.asyncio
async def test_builtin_echo_uses_quoted_arguments_and_templates() -> None:
    result = await execute_custom_command(
        "builtin_function",
        {"name": "echo", "template": "city={{arg0}} args={{args}}"},
        context('"New York" tomorrow'),
    )

    assert result == "city=New York args=\"New York\" tomorrow"


def test_executor_validation_rejects_unknown_builtin_and_unsafe_script() -> None:
    with pytest.raises(CustomCommandConfigError, match="已注册函数"):
        validate_executor_config("builtin_function", {"name": "callable_from_db"})
    with pytest.raises(CustomCommandConfigError, match="不允许 import"):
        validate_executor_config(
            "python", {"code": "import os\nprint('bad')", "allowExecution": True}, enabled=True
        )
    with pytest.raises(CustomCommandConfigError, match="显式设置 allowExecution"):
        validate_executor_config("python", {"code": "print('ok')"}, enabled=True)


def test_custom_command_service_rejects_enabled_none_executor() -> None:
    database = SimpleNamespace(
        list_bot_command_configs=lambda: [],
        upsert_bot_command_config=lambda *args, **kwargs: None,
    )

    with pytest.raises(BotCommandValidationError, match="必须配置可执行"):
        BotCommandService(database).create_command_config(
            "report", "生成报告", enabled=True, executor_type="none"
        )


@pytest.mark.asyncio
async def test_http_executor_rejects_private_target() -> None:
    with pytest.raises(CustomCommandExecutorError, match="内网或保留 IP"):
        await execute_custom_command(
            "http",
            {"url": "https://127.0.0.1/internal"},
            context(),
        )


@pytest.mark.asyncio
async def test_http_executor_formats_json_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            return httpx.Response(
                200,
                json={"data": {"summary": "sunny"}},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr("tg_botx.features.bot.executors.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr(
        "tg_botx.features.bot.executors._assert_safe_target",
        lambda *args: __import__("asyncio").sleep(0),
    )
    result = await execute_custom_command(
        "http",
        {
            "url": "https://example.com/weather?city={{arg0}}",
            "responseFormat": "json",
            "responsePath": "data.summary",
            "allowedHosts": ["example.com"],
        },
        context(),
    )
    assert result == "sunny"
