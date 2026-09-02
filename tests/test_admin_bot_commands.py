from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tg_botx.features.admin_bot import (
    BotCommandForbiddenError,
    BotManagementService,
    TelegramManagementBot,
)
from tg_botx.integrations.telegram_bot import TelegramBotApiClient


def test_command_configs_include_historical_custom_commands() -> None:
    database = SimpleNamespace(
        list_bot_command_configs=lambda: [
            SimpleNamespace(command="help", description="自定义帮助", enabled=True),
            SimpleNamespace(command="new", description="历史命令", enabled=True),
        ]
    )

    configs = BotManagementService(database, SimpleNamespace()).command_configs()

    assert [item["command"] for item in configs] == [
        "start",
        "help",
        "bind",
        "unbind",
        "tasks",
        "status",
        "new",
    ]
    assert configs[-1]["type"] == "custom"
    assert (
        next(item for item in configs if item["command"] == "help")["description"] == "自定义帮助"
    )


async def test_refresh_commands_replaces_private_menu_and_clears_group_history() -> None:
    client = SimpleNamespace(set_commands=AsyncMock(), delete_commands=AsyncMock())
    bot = object.__new__(TelegramManagementBot)
    bot.client = client
    bot.management = SimpleNamespace(
        command_configs=lambda: [
            {"command": "start", "description": "查看绑定状态", "enabled": True},
            {"command": "help", "description": "查看帮助", "enabled": False},
        ]
    )

    await bot.refresh_commands()

    commands = [{"command": "start", "description": "查看绑定状态"}]
    assert client.set_commands.await_args_list == [
        ((commands,), {"scope": {"type": "default"}}),
        ((commands,), {"scope": {"type": "all_private_chats"}}),
    ]
    assert client.delete_commands.await_args_list == [
        ((), {"scope": {"type": "all_group_chats"}}),
        ((), {"scope": {"type": "all_chat_administrators"}}),
    ]


async def test_refresh_commands_deletes_managed_scopes_when_all_commands_disabled() -> None:
    client = SimpleNamespace(set_commands=AsyncMock(), delete_commands=AsyncMock())
    bot = object.__new__(TelegramManagementBot)
    bot.client = client
    bot.management = SimpleNamespace(
        command_configs=lambda: [
            {"command": "help", "description": "查看帮助", "enabled": False},
        ]
    )

    await bot.refresh_commands()

    client.set_commands.assert_not_awaited()
    assert client.delete_commands.await_args_list == [
        ((), {"scope": {"type": "default"}}),
        ((), {"scope": {"type": "all_private_chats"}}),
        ((), {"scope": {"type": "all_group_chats"}}),
        ((), {"scope": {"type": "all_chat_administrators"}}),
    ]


async def test_telegram_command_api_sends_scope_and_language() -> None:
    client = object.__new__(TelegramBotApiClient)
    client.call = AsyncMock(return_value=[])
    scope = {"type": "all_private_chats"}

    await client.set_commands(
        [{"command": "help", "description": "Help"}],
        scope=scope,
        language_code="en",
    )
    await client.get_commands(scope=scope, language_code="en")
    await client.delete_commands(scope=scope, language_code="en")

    assert client.call.await_args_list == [
        (
            (
                "setMyCommands",
                {
                    "commands": [{"command": "help", "description": "Help"}],
                    "scope": scope,
                    "language_code": "en",
                },
            ),
            {},
        ),
        (("getMyCommands", {"scope": scope, "language_code": "en"}), {}),
        (("deleteMyCommands", {"scope": scope, "language_code": "en"}), {}),
    ]


def test_create_custom_command_persists_reserved_executor_shape() -> None:
    rows: list[SimpleNamespace] = []

    def upsert(command, description, enabled, allowed_roles_json, **kwargs):
        item = SimpleNamespace(
            command=command,
            description=description,
            enabled=enabled,
            allowed_roles_json=allowed_roles_json,
            command_type=kwargs["command_type"],
            executor_type=kwargs["executor_type"],
            executor_config_json=kwargs["executor_config_json"],
        )
        rows.append(item)
        return item

    database = SimpleNamespace(list_bot_command_configs=lambda: rows, upsert_bot_command_config=upsert)
    item = BotManagementService(database, SimpleNamespace()).create_command_config(
        "/report", "生成报告"
    )

    assert item["type"] == "custom"
    assert item["executorType"] == "none"
    assert item["executorConfig"] == {}
    assert rows[0].enabled is False


def test_system_command_cannot_be_deleted() -> None:
    database = SimpleNamespace(list_bot_command_configs=lambda: [])
    service = BotManagementService(database, SimpleNamespace())

    with pytest.raises(BotCommandForbiddenError):
        service.delete_command_config("help")
