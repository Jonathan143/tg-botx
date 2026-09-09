from types import SimpleNamespace

import httpx
import pytest

from tg_botx.features.bot.commands import BotCommandService
from tg_botx.features.bot.executors import (
    CommandConfigError,
    CommandExecutionError,
    execute_command,
    validate_executor_config,
)
from tg_botx.features.bot.models import BotCommandValidationError


async def test_builtin_executor_is_whitelisted() -> None:
    assert await execute_command("builtin_function", {"name": "echo"}, "hello") == "hello"
    assert await execute_command("builtin_function", {"name": "uppercase"}, "hello") == "HELLO"

    with pytest.raises(CommandExecutionError, match="未注册"):
        await execute_command("builtin_function", {"name": "__import__"}, "hello")


def test_unsafe_and_invalid_executor_configs_are_rejected_before_persistence() -> None:
    rows: list[SimpleNamespace] = []

    def upsert(*args, **kwargs):
        item = SimpleNamespace(
            command=args[0],
            description=args[1],
            enabled=args[2],
            allowed_roles_json=args[3],
            command_type=kwargs["command_type"],
            executor_type=kwargs["executor_type"],
            executor_config_json=kwargs["executor_config_json"],
        )
        rows.append(item)
        return item

    service = BotCommandService(
        SimpleNamespace(list_bot_command_configs=lambda: rows, upsert_bot_command_config=upsert)
    )

    with pytest.raises(BotCommandValidationError, match="python.*暂未开放"):
        service.create_command_config("run", "运行代码", executor_type="python")
    with pytest.raises(BotCommandValidationError, match="内网"):
        service.create_command_config(
            "hook", "内部回调", executor_type="http", executor_config={"url": "http://127.0.0.1"}
        )
    assert rows == []


def test_legacy_unsupported_commands_are_exposed_as_disabled() -> None:
    legacy = SimpleNamespace(
        command="legacy",
        description="旧脚本",
        enabled=True,
        command_type="custom",
        executor_type="javascript",
        executor_config_json="{}",
        allowed_roles_json="[]",
    )
    service = BotCommandService(SimpleNamespace(list_bot_command_configs=lambda: [legacy]))

    item = next(item for item in service.command_configs() if item["command"] == "legacy")
    assert item["enabled"] is False
    assert "javascript" in item["executorError"]


@pytest.mark.asyncio
async def test_http_executor_posts_argument_and_retries_transient_errors(monkeypatch) -> None:
    client_type = httpx.AsyncClient
    requests: list[httpx.Request] = []
    responses = iter(
        [
            httpx.Response(503, request=httpx.Request("POST", "https://example.com/hook")),
            httpx.Response(
                200,
                text="<done>",
                request=httpx.Request("POST", "https://example.com/hook"),
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    monkeypatch.setattr(
        "tg_botx.features.bot.executors._ensure_public_dns_target", lambda _: _noop()
    )
    monkeypatch.setattr(
        "tg_botx.features.bot.executors.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )

    result = await execute_command(
        "http",
        {"url": "https://example.com/hook", "method": "POST", "retries": 1},
        "hello",
    )

    assert result == "<done>"
    assert len(requests) == 2
    assert requests[0].content == b'{"argument":"hello"}'


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_http_executor_rejects_failed_response_without_leaking_body(monkeypatch) -> None:
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "tg_botx.features.bot.executors._ensure_public_dns_target", lambda _: _noop()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="secret upstream details", request=request)

    monkeypatch.setattr(
        "tg_botx.features.bot.executors.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )

    with pytest.raises(CommandExecutionError, match="HTTP 400") as exc_info:
        await execute_command("http", {"url": "https://example.com/hook"}, "hello")
    assert "secret upstream details" not in str(exc_info.value)


def test_public_url_validation_rejects_private_targets() -> None:
    with pytest.raises(CommandConfigError, match="内网"):
        validate_executor_config("http", {"url": "http://10.0.0.1/hook"})
