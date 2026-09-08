from __future__ import annotations

import asyncio
import hmac
import logging
from collections import deque
from contextlib import suppress
from datetime import datetime
from typing import Any

from tg_botx.config import Settings
from tg_botx.features.bot.management import BotManagementService
from tg_botx.features.bot.models import (
    _COMMAND_NAME_PATTERN,
    _WEBHOOK_UPDATE_DEDUPE_LIMIT,
    DEFAULT_BOT_COMMANDS,
    BotRuntimeStatus,
)
from tg_botx.features.checkin.runtime import (
    CheckinService,
)
from tg_botx.infrastructure.persistence.db import (
    Database,
    utc_now,
)
from tg_botx.integrations.telegram_bot import TelegramBotApiClient, TelegramBotApiError
from tg_botx.interfaces.telegram.handlers import BotMessageHandlers

logger = logging.getLogger(__name__)


class TelegramManagementBot:
    def __init__(self, settings: Settings, database: Database, checkin: CheckinService):
        token = (
            settings.admin_bot_token.get_secret_value().strip() if settings.admin_bot_token else ""
        )
        self.database = database
        self.checkin = checkin
        self.management = BotManagementService(database, checkin)
        self.client = TelegramBotApiClient(token) if token else None
        # Keep the adapter tolerant of older injected settings objects.  The
        # concrete ``Settings`` model always exposes these fields, while
        # lightweight callers may still only provide the long-polling config.
        self.transport = getattr(settings, "bot_transport", "long_polling")
        self.webhook_url = getattr(settings, "bot_webhook_url", None)
        webhook_secret = getattr(settings, "bot_webhook_secret", None)
        if webhook_secret:
            get_secret_value = getattr(webhook_secret, "get_secret_value", None)
            raw_secret = get_secret_value() if callable(get_secret_value) else webhook_secret
            self._webhook_secret = raw_secret.strip() if isinstance(raw_secret, str) else ""
        else:
            self._webhook_secret = ""
        webhook_configured = bool(self.webhook_url and self._webhook_secret)
        configured = bool(token) and (self.transport != "webhook" or webhook_configured)
        self.status = BotRuntimeStatus(settings.bot_enabled, configured)
        self.handlers = BotMessageHandlers(
            database, checkin, self.management, self.client, self.status
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._offset: int | None = None
        self._webhook_update_lock = asyncio.Lock()
        self._webhook_seen_update_ids: set[int] = set()
        self._webhook_seen_update_order: deque[int] = deque()

    async def start(self) -> None:
        if not self.status.enabled:
            logger.info("TG_BOT_BOT_ENABLED=false，Telegram 管理 Bot 未启动")
            return
        if self.client is None:
            logger.error("未配置 TG_BOT_ADMIN_BOT_TOKEN，Telegram 管理 Bot 已禁用")
            return
        if self.transport == "webhook" and not self.status.configured:
            logger.error(
                "Webhook 模式需要配置 TG_BOT_BOT_WEBHOOK_URL 和 "
                "TG_BOT_BOT_WEBHOOK_SECRET，Telegram 管理 Bot 已禁用"
            )
            return
        if self._task is not None and not self._task.done():
            return
        # ``setWebhook``/``deleteWebhook`` both discard updates accumulated
        # before this session.  A new session therefore must not inherit the
        # previous in-memory duplicate window.
        self._webhook_seen_update_ids.clear()
        self._webhook_seen_update_order.clear()
        self.status.running = False
        self._stop.clear()
        # Polling offsets are intentionally session-scoped. Both transports
        # ask Telegram to discard updates accumulated before startup.
        self._offset = None
        if self.transport == "webhook":
            self._task = asyncio.create_task(
                self._webhook_registration_loop(), name="telegram-management-bot-webhook"
            )
        else:
            self._task = asyncio.create_task(
                self._poll_loop(), name="telegram-management-bot-polling"
            )

    def command_configs(self) -> list[dict[str, Any]]:
        return self.management.command_configs()

    async def pull_remote_commands(self) -> list[dict[str, Any]]:
        """Pull Telegram's default command menu into the local database."""
        if self.client is None:
            return self.command_configs()
        remote = await self.client.get_commands(scope={"type": "default"})
        remote_by_name: dict[str, str] = {}
        for item in remote:
            command = item["command"].casefold().removeprefix("/")
            description = item["description"].strip()
            if _COMMAND_NAME_PATTERN.fullmatch(command) and description:
                remote_by_name[command] = description
        default_names = {name for name, _ in DEFAULT_BOT_COMMANDS}
        current_configs = {item["command"]: item for item in self.command_configs()}
        for command, default_description in DEFAULT_BOT_COMMANDS:
            description = remote_by_name.get(command, "")
            if not description:
                current = current_configs.get(command)
                description = current["description"] if current is not None else default_description
                menu_visible = False
            else:
                menu_visible = True
            self.management.update_command_config(
                command,
                description,
                current_configs.get(command, {}).get("enabled", True),
                menu_visible=menu_visible,
            )
        for command, description in remote_by_name.items():
            # Telegram synchronization only owns the built-in command set;
            # custom commands are managed exclusively from the admin UI.
            if command in default_names:
                current = current_configs.get(command, {})
                self.management.update_command_config(
                    command,
                    description,
                    current.get("enabled", True),
                    menu_visible=True,
                )
        return self.command_configs()

    async def sync_remote_commands(self) -> list[dict[str, Any]]:
        """Backward-compatible alias for the explicit pull operation."""
        return await self.pull_remote_commands()

    async def refresh_commands(self) -> None:
        """Push the configured command menu to Telegram immediately."""
        if self.client is None:
            return
        commands = [
            {"command": item["command"], "description": item["description"]}
            for item in self.command_configs()
            if BotManagementService._menu_visible(item)
        ]
        # Telegram resolves a private-chat scope before the default scope. A
        # stale private-chat menu therefore masks a newly configured default
        # menu. Keep both managed scopes identical so omitted commands are
        # replaced in either location.
        managed_scopes: tuple[dict[str, object], ...] = (
            {"type": "default"},
            {"type": "all_private_chats"},
        )
        for scope in managed_scopes:
            if commands:
                await self.client.set_commands(commands, scope=scope)
            else:
                await self.client.delete_commands(scope=scope)

        # This bot ignores non-private messages. Remove historical menus from
        # group scopes instead of advertising commands that cannot run there.
        unused_scopes: tuple[dict[str, object], ...] = (
            {"type": "all_group_chats"},
            {"type": "all_chat_administrators"},
        )
        for scope in unused_scopes:
            await self.client.delete_commands(scope=scope)

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.client is not None:
            await self.client.close()

    def public_status(self) -> dict[str, Any]:
        return {
            "enabled": self.status.enabled,
            "configured": self.status.configured,
            "running": self.status.running,
            "health": self.status.health,
            "transport": self.transport,
            "lastPollAt": self.status.last_poll_at.isoformat()
            if self.status.last_poll_at
            else None,
            "lastError": self.status.last_error,
        }

    def webhook_secret_matches(self, secret: str | None) -> bool:
        """Return whether a request carries this bot's configured secret."""

        configured_secret = getattr(self, "_webhook_secret", "")
        if (
            getattr(self, "transport", None) != "webhook"
            or not isinstance(configured_secret, str)
            or not configured_secret
        ):
            return False
        if not isinstance(secret, str) or not secret or len(secret) > 256:
            return False
        try:
            candidate = secret.encode("ascii")
        except UnicodeEncodeError:
            # ``hmac.compare_digest(str, str)`` raises for non-ASCII input;
            # an invalid header should be a normal authentication failure.
            return False
        try:
            expected = configured_secret.encode("ascii")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(candidate, expected)

    def accepts_webhook(self, secret: str | None) -> bool:
        """Authenticate and authorize an update for normal processing."""

        stop_event = getattr(self, "_stop", None)
        if stop_event is not None and stop_event.is_set():
            return False
        return (
            self.status.enabled
            and self.status.configured
            # Do not accept Telegram retries until setWebhook has completed
            # with drop_pending_updates=True. This keeps commands sent while
            # the service was offline from slipping through during startup.
            and self.status.running
            and self.webhook_secret_matches(secret)
        )

    def _claim_webhook_update(self, update: dict[str, object]) -> bool:
        """Claim an update id once, retaining only a bounded recent window."""

        update_id = update.get("update_id")
        if type(update_id) is not int:
            return True
        seen = getattr(self, "_webhook_seen_update_ids", None)
        if seen is None:
            seen = set()
            self._webhook_seen_update_ids = seen
        if update_id in seen:
            return False
        seen.add(update_id)
        order = getattr(self, "_webhook_seen_update_order", None)
        if order is None:
            order = deque()
            self._webhook_seen_update_order = order
        order.append(update_id)
        while len(order) > _WEBHOOK_UPDATE_DEDUPE_LIMIT:
            seen.discard(order.popleft())
        return True

    def _webhook_update_guard(self) -> asyncio.Lock:
        lock = getattr(self, "_webhook_update_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._webhook_update_lock = lock
        return lock

    async def discard_webhook_update(self, update: dict[str, object]) -> None:
        """Acknowledge an authenticated update without executing it."""

        async with self._webhook_update_guard():
            self._claim_webhook_update(update)

    async def handle_webhook_update(self, update: dict[str, object]) -> None:
        """Process one authenticated Telegram webhook update."""

        async with self._webhook_update_guard():
            # The route performs the normal readiness check before parsing the
            # body, but shutdown can begin while a request is waiting for this
            # lock.  Re-check the stop gate here so queued requests are
            # acknowledged and discarded instead of starting during teardown.
            stop_event = getattr(self, "_stop", None)
            if stop_event is not None and stop_event.is_set():
                self._claim_webhook_update(update)
                return
            if not self._claim_webhook_update(update):
                return
            self.status.last_poll_at = utc_now()
            try:
                await self._handle_update(update)
                self.status.last_error = None
            except Exception as exc:
                self.status.last_error = type(exc).__name__
                logger.exception("管理 Bot 处理 Webhook update 失败 type=%s", type(exc).__name__)

    async def _webhook_registration_loop(self) -> None:
        assert self.client is not None
        assert self.webhook_url is not None
        try:
            while not self._stop.is_set():
                try:
                    await self.client.set_webhook(self.webhook_url, self._webhook_secret)
                    if self._stop.is_set():
                        break
                    self.status.running = True
                    self.status.last_error = None
                    logger.info("Telegram 管理 Bot 已使用 Webhook 模式启动")
                    await self._stop.wait()
                except asyncio.CancelledError:
                    raise
                except TelegramBotApiError as exc:
                    self.status.running = False
                    self.status.last_error = type(exc).__name__
                    logger.warning("管理 Bot 注册 Webhook 失败")
                    await asyncio.sleep(min(exc.retry_after or 5, 30))
                except Exception as exc:
                    self.status.running = False
                    self.status.last_error = type(exc).__name__
                    logger.warning("管理 Bot 注册 Webhook 失败 type=%s", type(exc).__name__)
                    await asyncio.sleep(5)
        finally:
            self.status.running = False

    async def _poll_loop(self) -> None:
        assert self.client is not None
        self.status.running = True
        try:
            # getUpdates and Webhook are mutually exclusive. Remove any
            # previously registered Webhook and discard stale interactive
            # commands before entering the polling loop.
            while not self._stop.is_set():
                try:
                    await self.client.delete_webhook(drop_pending_updates=True)
                    break
                except asyncio.CancelledError:
                    raise
                except TelegramBotApiError as exc:
                    self.status.last_error = type(exc).__name__
                    await asyncio.sleep(min(exc.retry_after or 5, 30))
                except Exception as exc:
                    self.status.last_error = type(exc).__name__
                    logger.warning("管理 Bot 启动清理失败 type=%s", type(exc).__name__)
                    await asyncio.sleep(5)
            while not self._stop.is_set():
                try:
                    updates = await self.client.get_updates(self._offset)
                    self.status.last_poll_at = utc_now()
                    self.status.last_error = None
                    for update in updates:
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            self._offset = update_id + 1
                        try:
                            await self._handle_update(update)
                        except Exception as exc:
                            self.status.last_error = type(exc).__name__
                            logger.exception(
                                "管理 Bot 处理 update 失败 type=%s", type(exc).__name__
                            )
                except asyncio.CancelledError:
                    raise
                except TelegramBotApiError as exc:
                    self.status.last_error = type(exc).__name__
                    await asyncio.sleep(min(exc.retry_after or 5, 30))
                except Exception as exc:
                    self.status.last_error = type(exc).__name__
                    logger.warning("管理 Bot 轮询失败 type=%s", type(exc).__name__)
                    await asyncio.sleep(5)
        finally:
            self.status.running = False

    async def _handle_update(self, update: dict[str, object]) -> None:
        return await self.handlers._handle_update(update)

    async def _handle_message(self, message: dict[str, object], update_id: object) -> None:
        return await self.handlers._handle_message(message, update_id)

    async def _bind(
        self, chat_id: int, user_id: int, user: dict[str, object], code: str, update_id: int | None
    ) -> None:
        return await self.handlers._bind(chat_id, user_id, user, code, update_id)

    async def _handle_callback(self, callback: dict[str, object], update_id: object) -> None:
        return await self.handlers._handle_callback(callback, update_id)

    def _authorized(self, user_id: int, chat_id: int, update_id: int | None, action: str) -> bool:
        return self.handlers._authorized(user_id, chat_id, update_id, action)

    def _command_allowed(
        self, user_id: int, chat_id: int, command: str, update_id: int | None
    ) -> bool:
        return self.handlers._command_allowed(user_id, chat_id, command, update_id)

    async def _send_tasks(self, chat_id: int, page: int) -> None:
        return await self.handlers._send_tasks(chat_id, page)

    async def _edit_tasks(self, chat_id: int, message_id: int, page: int) -> None:
        return await self.handlers._edit_tasks(chat_id, message_id, page)

    def _task_page(self, page: int) -> tuple[str, dict[str, object]]:
        return self.handlers._task_page(page)

    async def _edit_task(self, chat_id: int, message_id: int, task_id: str) -> None:
        return await self.handlers._edit_task(chat_id, message_id, task_id)

    def _task_detail(self, task_id: str) -> tuple[str, dict[str, object]]:
        return self.handlers._task_detail(task_id)

    async def _edit_confirmation(
        self, chat_id: int, message_id: int, action: str, task_id: str, _: str
    ) -> None:
        return await self.handlers._edit_confirmation(chat_id, message_id, action, task_id, _)

    async def _perform_action(
        self,
        user_id: int,
        chat_id: int,
        message_id: int,
        action: str,
        task_id: str,
        expires: str,
        update_id: int | None,
    ) -> None:
        return await self.handlers._perform_action(
            user_id, chat_id, message_id, action, task_id, expires, update_id
        )

    async def _send(self, chat_id: int, text: str, markup: dict[str, object] | None = None) -> None:
        return await self.handlers._send(chat_id, text, markup)

    def _welcome(self, user_id: int, chat_id: int) -> str:
        return self.handlers._welcome(user_id, chat_id)

    def _help(self, user_id: int | None = None, chat_id: int | None = None) -> str:
        return self.handlers._help(user_id, chat_id)

    def _system_status(self) -> str:
        return self.handlers._system_status()

    @staticmethod
    def _ids(user: dict[str, object], chat: dict[str, object]) -> tuple[int | None, int | None]:
        return BotMessageHandlers._ids(user, chat)

    @staticmethod
    def _page(value: str) -> int:
        return BotMessageHandlers._page(value)

    @staticmethod
    def _format_time(value: datetime | None, timezone_name: str) -> str:
        return BotMessageHandlers._format_time(value, timezone_name)
