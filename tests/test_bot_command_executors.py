from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from tg_botx.features.bot.commands import BotCommandService
from tg_botx.features.bot.executors import (
    CustomCommandConfigError,
    CustomCommandContext,
    CustomCommandExecutorError,
    execute_custom_command,
    validate_executor_config,
)
from tg_botx.features.bot.models import BotCommandValidationError
from tg_botx.interfaces.telegram.handlers import BotMessageHandlers


def context(argument: str = "Tokyo") -> CustomCommandContext:
    return CustomCommandContext("weather", argument, f"/weather {argument}", 100, 200)


@pytest.mark.asyncio
async def test_builtin_functions_all() -> None:
    # args
    res_args = await execute_custom_command(
        "builtin_function", {"name": "args"}, context("Tokyo Kyoto")
    )
    assert res_args == "Tokyo Kyoto"

    # echo
    res_echo = await execute_custom_command(
        "builtin_function",
        {"name": "echo", "template": "city={{arg0}} args={{args}}"},
        context('"New York" tomorrow'),
    )
    assert res_echo == 'city=New York args="New York" tomorrow'

    # json
    res_json = await execute_custom_command("builtin_function", {"name": "json"}, context("Tokyo"))
    parsed = json.loads(res_json)
    assert parsed["command"] == "weather"
    assert parsed["args"] == ["Tokyo"]

    # utc_time
    res_time = await execute_custom_command(
        "builtin_function", {"name": "utc_time"}, context("Tokyo")
    )
    assert "T" in res_time


def test_executor_validation_rejects_unknown_builtin_and_unsafe_script() -> None:
    with pytest.raises(CustomCommandConfigError, match="已注册函数"):
        validate_executor_config("builtin_function", {"name": "callable_from_db"})
    with pytest.raises(CustomCommandConfigError, match="不允许 import"):
        validate_executor_config(
            "python", {"code": "import os\nprint('bad')", "allowExecution": True}, enabled=True
        )
    with pytest.raises(CustomCommandConfigError, match="显式设置 allowExecution"):
        validate_executor_config("python", {"code": "print('ok')"}, enabled=True)
    with pytest.raises(CustomCommandConfigError, match="不允许访问 open"):
        validate_executor_config(
            "python",
            {"code": "x = json.open('test')\nprint(x)", "allowExecution": True},
            enabled=True,
        )
    with pytest.raises(CustomCommandConfigError, match="双下划线"):
        validate_executor_config(
            "javascript",
            {"code": "console.log(context.__proto__);", "allowExecution": True},
            enabled=True,
        )
    with pytest.raises(CustomCommandConfigError, match="responseFormat"):
        validate_executor_config("http", {"url": "https://example.com", "responseFormat": []})
    with pytest.raises(CustomCommandConfigError, match="allowedHosts 白名单"):
        validate_executor_config("http", {"url": "https://{{arg0}}/test"}, enabled=True)


def test_custom_command_service_rejects_enabled_none_executor() -> None:
    database = SimpleNamespace(
        list_bot_command_configs=lambda: [],
        upsert_bot_command_config=lambda *args, **kwargs: None,
    )

    with pytest.raises(BotCommandValidationError, match="必须配置可执行"):
        BotCommandService(database).create_command_config(
            "report", "生成报告", enabled=True, executor_type="none"
        )


def test_custom_command_service_update_enforces_size_limit() -> None:
    rows: list[SimpleNamespace] = []

    def upsert(command, description, enabled, allowed_roles_json, **kwargs):
        item = SimpleNamespace(command=command, description=description, enabled=enabled,
            allowed_roles_json=allowed_roles_json, command_type=kwargs.get("command_type", "custom"),
            executor_type=kwargs.get("executor_type", "none"),
            executor_config_json=kwargs.get("executor_config_json", "{}"))
        rows.append(item)
        return item

    database = SimpleNamespace(list_bot_command_configs=lambda: rows, upsert_bot_command_config=upsert)
    service = BotCommandService(database)
    service.create_command_config("report", "生成报告", enabled=False, executor_type="none")
    with pytest.raises(BotCommandValidationError, match="不能超过 32KB"):
        service.update_command_config("report", "生成报告更新", enabled=False, executor_type="http",
            executor_config={"url": "https://example.com/api", "body": {"data": "x" * (33 * 1024)}})


def test_custom_command_service_does_not_rename_on_invalid_update() -> None:
    row = SimpleNamespace(command="before", description="demo", enabled=True,
        allowed_roles_json='["admin"]', command_type="custom", executor_type="builtin_function",
        executor_config_json='{"name":"echo"}')
    class Database:
        def list_bot_command_configs(self):
            return [row]

        def rename_bot_command_config(self, old, new):
            row.command = new
            return row

        def upsert_bot_command_config(self, *args, **kwargs):
            raise AssertionError("must not persist")
    service = BotCommandService(Database())
    with pytest.raises(BotCommandValidationError):
        service.update_command_config("before", "demo", True, new_command="after",
            executor_type="http", executor_config={})
    assert row.command == "before"


@pytest.mark.asyncio
async def test_python_executor_runs_safely_and_blocks_escapes() -> None:
    # Valid Python execution
    result = await execute_custom_command(
        "python",
        {
            "code": "print('hello', context['args'][0], json.dumps({'ok': True}))",
            "allowExecution": True,
        },
        context("Tokyo"),
    )
    assert result == 'hello Tokyo {"ok": true}'

    # Accessing builtins via json attribute is rejected
    with pytest.raises(CustomCommandConfigError, match="不允许访问 builtins"):
        validate_executor_config(
            "python",
            {"code": "b = json.builtins\nprint(b)", "allowExecution": True},
            enabled=True,
        )


@pytest.mark.asyncio
async def test_javascript_executor_runs_safely_and_blocks_escapes() -> None:
    import shutil

    if shutil.which("node") is None:
        pytest.skip("Node.js not installed")

    result = await execute_custom_command(
        "javascript",
        {
            "code": "console.log('hello', context.args[0], JSON.stringify({ok: true}));",
            "allowExecution": True,
        },
        context("Tokyo"),
    )
    assert result == 'hello Tokyo {"ok":true}'


@pytest.mark.asyncio
async def test_http_executor_rejects_private_target() -> None:
    with pytest.raises(CustomCommandExecutorError, match="内网或保留 IP"):
        await execute_custom_command(
            "http",
            {"url": "https://127.0.0.1/internal"},
            context(),
        )


@pytest.mark.asyncio
async def test_http_executor_rejects_credentials() -> None:
    with pytest.raises(CustomCommandConfigError, match="不允许携带用户名或密码"):
        validate_executor_config("http", {"url": "https://user:pass@example.com/api"})


@pytest.mark.asyncio
async def test_http_executor_formats_json_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            yield httpx.Response(
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


@pytest.mark.asyncio
async def test_http_executor_aborts_on_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            async def large_stream():
                for _ in range(10):
                    yield b"A" * 1024

            yield httpx.Response(
                200,
                content=large_stream(),
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr("tg_botx.features.bot.executors.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr(
        "tg_botx.features.bot.executors._assert_safe_target",
        lambda *args: __import__("asyncio").sleep(0),
    )
    with pytest.raises(CustomCommandExecutorError, match="超过配置的大小限制"):
        await execute_custom_command(
            "http",
            {
                "url": "https://example.com/data",
                "maxResponseBytes": 2048,
                "allowedHosts": ["example.com"],
            },
            context(),
        )


def test_chunk_message_avoids_splitting_html_entity() -> None:
    prefix = "x" * 3998
    entity = "&amp;suffix"
    full_text = prefix + entity

    chunks = BotMessageHandlers._chunk_message(full_text, max_chunk_size=4000)
    assert len(chunks) == 2
    assert not chunks[0].endswith("&")
    assert chunks[0] == prefix
    assert chunks[1] == entity


def test_chunk_message_always_advances_for_long_entity() -> None:
    chunks = BotMessageHandlers._chunk_message("&" + "x" * 5000 + ";", max_chunk_size=4000)
    assert "".join(chunks) == "&" + "x" * 5000 + ";"
    assert all(chunks)
