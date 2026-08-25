from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from telethon import TelegramClient

from tg_bot.config import Settings
from tg_bot.db import Account, Database, Task, TaskRun, utc_now
from tg_bot.executor import CheckinExecutor, run_with_retries
from tg_bot.schedule import next_run_for, schedule_from_task

logger = logging.getLogger(__name__)


class ClientPool:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.clients: dict[str, TelegramClient] = {}

    async def get(self, account: Account) -> TelegramClient:
        if account.id in self.clients:
            return self.clients[account.id]
        api_id, api_hash = self.settings.require_api_credentials()
        client = TelegramClient(str(self.settings.sessions_dir / account.session_name), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(f"账号 {account.name} 尚未登录，请先执行 tg-bot login")
        self.clients[account.id] = client
        return client

    async def close(self) -> None:
        for client in self.clients.values():
            await client.disconnect()
        self.clients.clear()


class NotificationService:
    """Send best-effort administrator notifications through Telegram Bot API."""

    _MAX_ATTEMPTS = 3
    _RETRY_DELAYS = (1, 2)

    def __init__(self, settings: Settings):
        self.settings = settings
        secret = settings.notification_bot_token
        self._token = secret.get_secret_value().strip() if secret else ""
        self._chat_id = settings.notification_chat_id
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
        configured = task.config.get("notify_bot_response")
        if configured is not None:
            return bool(configured)
        return bool(task.config.get("output_bot_response", False))

    @staticmethod
    def _message_chunks(text: str, limit: int = 4000) -> list[str]:
        return [text[index : index + limit] for index in range(0, len(text), limit)]

    @staticmethod
    def _task_time(task: Task, value: datetime | None) -> str:
        if value is None:
            return "未安排"
        try:
            zone = ZoneInfo(task.timezone)
        except ZoneInfoNotFoundError:
            zone = timezone.utc
        return value.astimezone(zone).isoformat(timespec="seconds") + f" ({task.timezone})"

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
    ) -> None:
        lines = [
            f"[{level}] {title}",
            f"任务：{task.name}",
            f"目标：{task.target}",
            f"时间：{self._task_time(task, utc_now())}",
        ]
        if error:
            lines.append(f"原因：{error}")
        if include_next_run:
            lines.append(f"下次计划：{self._task_time(task, next_run)}")
        if bot_response is not None and self._include_response(task):
            lines.extend(("机器人回复：", bot_response))
        await self._send_text("\n".join(lines))

    async def success(self, task: Task, next_run: datetime | None, bot_response: str | None) -> None:
        if self._is_enabled(task, "success"):
            await self._task_event(task, "INFO", "签到成功", next_run, bot_response=bot_response)

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
                "签到失败",
                next_run,
                error=error,
                bot_response=bot_response,
            )

    async def skipped(self, task: Task, next_run: datetime | None) -> None:
        await self._task_event(task, "WARNING", "任务因目标聊天忙碌而跳过", next_run)

    async def cancel_requested(self, task: Task) -> None:
        await self._task_event(
            task,
            "INFO",
            "任务取消请求已提交",
            None,
            include_next_run=False,
        )

    async def canceled(self, task: Task, next_run: datetime | None, reason: str) -> None:
        await self._task_event(task, "INFO", "任务已取消", next_run, error=reason)

    async def service_started(self) -> None:
        await self._send_text(
            f"[INFO] 签到服务已启动\n时间：{utc_now().isoformat(timespec='seconds')}"
        )

    async def service_stopped(self, reason: str) -> None:
        await self._send_text(
            f"[INFO] 签到服务已停止\n时间：{utc_now().isoformat(timespec='seconds')}\n原因：{reason}"
        )

    async def service_failed(self, error_type: str) -> None:
        await self._send_text(
            f"[ERROR] 签到服务发生致命异常\n时间：{utc_now().isoformat(timespec='seconds')}"
            f"\n异常类型：{error_type}"
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class CheckinService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.pool = ClientPool(settings)
        self.notifications = NotificationService(settings)
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.running: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        self.settings.ensure_directories()
        self.database.create_all()
        self.scheduler.start()
        for task in self.database.list_tasks():
            if task.enabled and not task.archived:
                self._ensure_next_run(task)
                self._schedule_task(task)

    async def close(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.pool.close()
        await self.notifications.close()

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        stop_reason = "正常停止"
        installed_signals: list[signal.Signals] = []

        def request_stop(received: signal.Signals) -> None:
            nonlocal stop_reason
            stop_reason = f"收到 {received.name}"
            stop_event.set()

        for received in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(received, request_stop, received)
                installed_signals.append(received)
            except (NotImplementedError, RuntimeError):
                pass

        started = False
        failed = False
        try:
            await self.start()
            started = True
            logger.info("签到服务已启动")
            await self.notifications.service_started()
            await stop_event.wait()
        except asyncio.CancelledError:
            stop_reason = "运行循环被取消"
            raise
        except Exception as exc:
            failed = True
            await self.notifications.service_failed(type(exc).__name__)
            raise
        finally:
            if started and not failed:
                await self.notifications.service_stopped(stop_reason)
            await self.close()
            for received in installed_signals:
                loop.remove_signal_handler(received)

    def _ensure_next_run(self, task: Task) -> None:
        now = datetime.now(timezone.utc)
        if task.next_run_at is None or task.next_run_at <= now:
            next_run = next_run_for(schedule_from_task(task), now=now)
            self.database.update_task(task.id, next_run_at=next_run)
            task.next_run_at = next_run

    def _schedule_task(self, task: Task) -> None:
        if task.next_run_at is None:
            return
        self.scheduler.add_job(
            self._scheduled_run,
            trigger=DateTrigger(run_date=task.next_run_at),
            args=[task.id],
            id=f"task:{task.id}",
            replace_existing=True,
            misfire_grace_time=None,
        )

    async def _scheduled_run(self, task_id: str) -> None:
        await self.run_task(task_id)

    async def _watch_cancellation(self, task_id: str, running: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(1)
            task = self.database.get_task(task_id)
            if task is None:
                return
            if task.cancel_requested:
                running.cancel()
                return

    @staticmethod
    def _log_bot_response_enabled(task: Task) -> bool:
        configured = task.config.get("log_bot_response")
        if configured is not None:
            return bool(configured)
        return bool(task.config.get("output_bot_response", False))

    async def _record_skipped(self, task: Task) -> None:
        finished = utc_now()
        next_run = next_run_for(schedule_from_task(task), now=finished)
        run = self.database.add_run(TaskRun(task_id=task.id, planned_at=task.next_run_at))
        self.database.update_run(
            run.id,
            finished_at=finished,
            status="skipped",
            attempts=0,
            error="目标聊天忙碌",
        )
        self.database.update_task(
            task.id,
            last_run_at=finished,
            last_status="skipped",
            next_run_at=next_run,
        )
        task.next_run_at = next_run
        await self.notifications.skipped(task, next_run)
        self._schedule_task(task)

    async def run_task(self, task_id: str) -> bool:
        task = self.database.get_task(task_id)
        if not task or task.archived or not task.enabled:
            raise RuntimeError("任务不存在、已归档或未启用")
        account = self.database.get_account_by_id(task.account_id)
        if not account:
            raise RuntimeError("任务绑定的账号不存在")
        lock_key = (account.id, task.target)
        lock = self.locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            logger.warning("任务 %s 因目标聊天忙碌而跳过本次执行", task.name)
            await self._record_skipped(task)
            return False

        async with lock:
            self.database.update_task(task.id, cancel_requested=False)
            run = self.database.add_run(TaskRun(task_id=task.id, planned_at=task.next_run_at))
            running = asyncio.current_task()
            if running is None:
                raise RuntimeError("无法获取当前任务实例")
            self.running[task.id] = running
            cancel_watcher = asyncio.create_task(self._watch_cancellation(task.id, running))
            bot_response: str | None = None
            try:
                client = await self.pool.get(account)

                def is_cancelled() -> bool:
                    current = self.database.get_task(task.id)
                    return bool(current and current.cancel_requested)

                success, error, attempts, bot_response = await run_with_retries(
                    CheckinExecutor(client, is_cancelled=is_cancelled), task
                )
                finished = utc_now()
                next_run = next_run_for(schedule_from_task(task), now=finished)
                self.database.update_run(
                    run.id,
                    finished_at=finished,
                    status="success" if success else "failed",
                    attempts=attempts,
                    error=error,
                )
                self.database.update_task(
                    task.id,
                    last_run_at=finished,
                    last_status="success" if success else "failed",
                    next_run_at=next_run,
                )
                task.next_run_at = next_run
                if self._log_bot_response_enabled(task) and bot_response is not None:
                    logger.info(
                        "机器人回复 task_id=%s name=%s status=%s\n%s",
                        task.id,
                        task.name,
                        "success" if success else "failed",
                        bot_response,
                    )
                if success:
                    await self.notifications.success(task, next_run, bot_response)
                else:
                    await self.notifications.failure(
                        task, error or "未知错误", next_run, bot_response
                    )
                self._schedule_task(task)
                return success
            except asyncio.CancelledError:
                finished = utc_now()
                current = self.database.get_task(task.id)
                requested = bool(current and current.cancel_requested)
                reason = "收到取消请求" if requested else "执行被中断"
                next_run = next_run_for(schedule_from_task(task), now=finished)
                self.database.update_run(
                    run.id, finished_at=finished, status="canceled", error=reason
                )
                self.database.update_task(
                    task.id,
                    last_run_at=finished,
                    last_status="canceled",
                    next_run_at=next_run,
                )
                task.next_run_at = next_run
                await self.notifications.canceled(task, next_run, reason)
                self._schedule_task(task)
                if not requested:
                    raise
                return False
            except Exception as exc:
                finished = utc_now()
                self.database.update_run(
                    run.id, finished_at=finished, status="failed", error=str(exc)
                )
                next_run = next_run_for(schedule_from_task(task), now=finished)
                self.database.update_task(
                    task.id,
                    last_run_at=finished,
                    last_status="failed",
                    next_run_at=next_run,
                )
                await self.notifications.failure(task, str(exc), next_run, bot_response)
                task.next_run_at = next_run
                self._schedule_task(task)
                return False
            finally:
                cancel_watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await cancel_watcher
                self.database.update_task(task.id, cancel_requested=False)
                self.running.pop(task.id, None)

    async def cancel_task(self, task_id: str) -> bool:
        task = self.database.get_task(task_id)
        if task is None:
            return False
        running = self.running.get(task.id)
        if running is None and not self.database.has_running_run(task.id):
            return False
        self.database.update_task(task.id, cancel_requested=True)
        await self.notifications.cancel_requested(task)
        if running is not None:
            running.cancel()
        return True
