from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from tg_botx.config import Settings
from tg_botx.core.time import format_local_time
from tg_botx.infrastructure.persistence.db import (
    Task,
    utc_now,
)
from tg_botx.integrations.client_pool import ClientPool as ClientPool

logger = logging.getLogger(__name__)


class NotificationService:
    """Send best-effort administrator notifications through Telegram Bot API."""

    _MAX_ATTEMPTS = 3
    _RETRY_DELAYS = (1, 2)
    _DIVIDER = "──────────────"
    _STATUS_ICONS = {
        "INFO": "ℹ️",
        "ERROR": "❌",
        "WARNING": "⚠️",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        secret = settings.notification_bot_token
        self._token = secret.get_secret_value().strip() if secret else ""
        self._chat_id = settings.notification_chat_id
        self._notification_timezone = settings.notification_timezone
        self._client: httpx.AsyncClient | None = None
        self._send_lock = asyncio.Lock()
        # httpx's INFO access log includes the full request URL.  Telegram Bot
        # API embeds the Token in that path, so it must never reach app logs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        if not self._token:
            logger.warning("未配置 TG_BOT_NOTIFICATION_BOT_TOKEN，Telegram 机器人通知已禁用")
        elif self._chat_id is None:
            logger.warning("未配置 TG_BOT_ADMIN_CHAT_IDS，Telegram 机器人通知已禁用")

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id is not None)

    @staticmethod
    def _is_enabled(task: Task, status: str) -> bool:
        notifications = task.config.get("notifications") or {}
        return bool(notifications.get(status, status == "failure"))

    @staticmethod
    def _include_response(task: Task) -> bool:
        return bool(task.config.get("notify_bot_response", False))

    @staticmethod
    def _message_chunks(text: str, limit: int = 4000) -> list[str]:
        return [text[index : index + limit] for index in range(0, len(text), limit)]

    @staticmethod
    def _format_time(value: datetime | None, timezone_name: str) -> str:
        return format_local_time(value, timezone_name, seconds=True)

    @staticmethod
    def _task_time(task: Task, value: datetime | None) -> str:
        return NotificationService._format_time(value, task.timezone)

    def _notification_time(self, value: datetime | None) -> str:
        return self._format_time(value, self._notification_timezone)

    async def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers={"User-Agent": "tg-checkin-bot/0.1"},
            )
        return self._client

    async def _send_chunk(self, text: str) -> bool:
        client = await self._http_client()
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            retryable = False
            retry_after: float | None = None
            try:
                response = await client.post(url, json=payload)
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                if response.status_code == 200 and body.get("ok") is True:
                    return True
                retryable = response.status_code == 429 or response.status_code >= 500
                if response.status_code == 429:
                    parameters = body.get("parameters") or {}
                    value = parameters.get("retry_after")
                    if isinstance(value, (int, float)):
                        retry_after = min(float(value), 30.0)
                logger.error(
                    "Telegram 机器人通知投递失败 status=%s attempt=%s",
                    response.status_code,
                    attempt,
                )
            except httpx.RequestError:
                retryable = True
                logger.error("Telegram 机器人通知网络异常 attempt=%s", attempt)

            if not retryable or attempt >= self._MAX_ATTEMPTS:
                return False
            delay = retry_after if retry_after is not None else self._RETRY_DELAYS[attempt - 1]
            await asyncio.sleep(delay)
        return False

    async def _send_text(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            async with self._send_lock:
                for chunk in self._message_chunks(text):
                    if not await self._send_chunk(chunk):
                        return
        except Exception as exc:
            # httpx exceptions can retain the request URL, whose path contains
            # the Bot Token.  Log only the exception type and never the URL.
            logger.error("Telegram 机器人通知发生未预期异常 type=%s", type(exc).__name__)

    async def _task_event(
        self,
        task: Task,
        level: str,
        title: str,
        next_run: datetime | None,
        *,
        error: str | None = None,
        bot_response: str | None = None,
        include_next_run: bool = True,
        icon: str | None = None,
    ) -> None:
        icon = icon or self._STATUS_ICONS.get(level, "📣")
        lines = [
            f"{icon} {title}",
            self._DIVIDER,
            f"📋 任务：{task.name}",
            f"🎯 目标：{task.target}",
            f"🕒 时间：{self._task_time(task, utc_now())}",
        ]
        if error:
            lines.append(f"📝 原因：{error}")
        if include_next_run:
            lines.append(f"⏭️ 下次计划：{self._task_time(task, next_run)}")
        if bot_response is not None and self._include_response(task):
            lines.extend((self._DIVIDER, "🤖 机器人回复：", bot_response))
        await self._send_text("\n".join(lines))

    async def success(
        self, task: Task, next_run: datetime | None, bot_response: str | None
    ) -> None:
        if self._is_enabled(task, "success"):
            await self._task_event(
                task, "INFO", "任务执行成功", next_run, bot_response=bot_response, icon="✅"
            )

    async def failure(
        self,
        task: Task,
        error: str,
        next_run: datetime | None,
        bot_response: str | None = None,
    ) -> None:
        if self._is_enabled(task, "failure"):
            await self._task_event(
                task,
                "ERROR",
                "任务执行失败",
                next_run,
                error=error,
                bot_response=bot_response,
                icon="❌",
            )

    async def skipped(self, task: Task, next_run: datetime | None) -> None:
        await self._task_event(task, "WARNING", "任务因目标聊天忙碌而跳过", next_run, icon="⏭️")

    async def cancel_requested(self, task: Task) -> None:
        await self._task_event(
            task,
            "INFO",
            "任务取消请求已提交",
            None,
            include_next_run=False,
            icon="🛑",
        )

    async def canceled(self, task: Task, next_run: datetime | None, reason: str) -> None:
        await self._task_event(task, "INFO", "任务已取消", next_run, error=reason, icon="🛑")

    async def service_started(self) -> None:
        if not self.settings.service_lifecycle_notifications_enabled:
            return
        await self._send_text(
            "\n".join(
                (
                    "🚀 签到服务已启动",
                    self._DIVIDER,
                    f"🕒 时间：{self._notification_time(utc_now())}",
                )
            )
        )

    async def service_stopped(self, reason: str) -> None:
        if not self.settings.service_lifecycle_notifications_enabled:
            return
        await self._send_text(
            "\n".join(
                (
                    "🛑 签到服务已停止",
                    self._DIVIDER,
                    f"🕒 时间：{self._notification_time(utc_now())}",
                    f"📝 原因：{reason}",
                )
            )
        )

    async def service_failed(self, error_type: str) -> None:
        await self._send_text(
            "\n".join(
                (
                    "💥 签到服务发生致命异常",
                    self._DIVIDER,
                    f"🕒 时间：{self._notification_time(utc_now())}",
                    f"📝 异常类型：{error_type}",
                )
            )
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
