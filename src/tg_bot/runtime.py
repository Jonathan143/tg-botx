from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
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
    def __init__(self, settings: Settings, pool: ClientPool):
        self.settings = settings
        self.pool = pool

    async def failure(self, account: Account, task: Task, error: str, next_run: datetime | None) -> None:
        if not self.settings.admin_chat_id_list:
            return
        try:
            client = await self.pool.get(account)
            text = (
                f"签到失败\n任务：{task.name}\n目标：{task.target}\n"
                f"时间：{utc_now().isoformat()}\n原因：{error}\n"
                f"下次计划：{next_run.isoformat() if next_run else '未安排'}"
            )
            for chat_id in self.settings.admin_chat_id_list:
                await client.send_message(chat_id, text)
        except Exception:
            logger.exception("发送失败通知时出错")


class CheckinService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.pool = ClientPool(settings)
        self.notifications = NotificationService(settings, self.pool)
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

    async def run_forever(self) -> None:
        await self.start()
        logger.info("签到服务已启动")
        try:
            await asyncio.Event().wait()
        finally:
            self.scheduler.shutdown(wait=False)
            await self.pool.close()

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
            return False

        async with lock:
            self.database.update_task(task.id, cancel_requested=False)
            run = self.database.add_run(TaskRun(task_id=task.id, planned_at=task.next_run_at))
            self.running[task.id] = asyncio.current_task()
            try:
                client = await self.pool.get(account)
                is_cancelled = lambda: bool(self.database.get_task(task.id).cancel_requested)
                success, error, attempts = await run_with_retries(
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
                if not success:
                    await self.notifications.failure(account, task, error or "未知错误", next_run)
                self._schedule_task(task)
                return success
            except asyncio.CancelledError:
                self.database.update_run(run.id, finished_at=utc_now(), status="canceled", error="手动取消")
                raise
            except Exception as exc:
                self.database.update_run(run.id, finished_at=utc_now(), status="failed", error=str(exc))
                self.database.update_task(task.id, last_run_at=utc_now(), last_status="failed")
                next_run = next_run_for(schedule_from_task(task), now=utc_now())
                self.database.update_task(task.id, next_run_at=next_run)
                await self.notifications.failure(account, task, str(exc), next_run)
                task.next_run_at = next_run
                self._schedule_task(task)
                return False
            finally:
                self.database.update_task(task.id, cancel_requested=False)
                self.running.pop(task.id, None)

    async def cancel_task(self, task_id: str) -> bool:
        running = self.running.get(task_id)
        if not running:
            task = self.database.get_task(task_id)
            if not task:
                return False
            self.database.update_task(task.id, cancel_requested=True)
            return True
        self.database.update_task(task_id, cancel_requested=True)
        running.cancel()
        return True
