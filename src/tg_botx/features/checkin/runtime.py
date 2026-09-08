from __future__ import annotations

import asyncio
import copy
import json
import logging
import signal
from contextlib import suppress
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from tg_botx.config import Settings
from tg_botx.features.checkin.errors import AccountNotFoundError as AccountNotFoundError
from tg_botx.features.checkin.errors import ManualRunConflict as ManualRunConflict
from tg_botx.features.checkin.errors import TaskNameConflictError as TaskNameConflictError
from tg_botx.features.checkin.errors import TaskNotFound as TaskNotFound
from tg_botx.features.checkin.errors import TaskStateError as TaskStateError
from tg_botx.features.checkin.errors import WorkflowVersionNotFound as WorkflowVersionNotFound
from tg_botx.features.checkin.executor import CheckinExecutor, run_with_retries
from tg_botx.features.checkin.notifications import NotificationService as NotificationService
from tg_botx.features.checkin.progress import ProgressTracker
from tg_botx.features.checkin.schedule import next_run_for, schedule_from_task
from tg_botx.features.checkin.scheduler import TaskScheduler
from tg_botx.features.checkin.tasks import TaskService
from tg_botx.features.checkin.workflows import WorkflowService
from tg_botx.infrastructure.persistence.db import (
    Account,
    Database,
    Task,
    TaskRun,
    WorkflowVersion,
    utc_now,
)
from tg_botx.integrations.client_pool import ClientPool as ClientPool
from tg_botx.schemas import TaskDefinition

logger = logging.getLogger(__name__)


class CheckinService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        pool: ClientPool | None = None,
        notifications: NotificationService | None = None,
        scheduler: AsyncIOScheduler | None = None,
    ):
        self.settings = settings
        self.database = database
        self.pool = pool if pool is not None else ClientPool(settings)
        self.notifications = (
            notifications if notifications is not None else NotificationService(settings)
        )
        self.scheduler = scheduler if scheduler is not None else AsyncIOScheduler(timezone="UTC")
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.running: dict[str, asyncio.Task[bool]] = {}
        self._manual_reservations: set[tuple[str, str]] = set()
        # In-memory generation counters distinguish administrator scheduling
        # mutations from execution-status writes without a schema migration.
        self._task_revisions: dict[str, int] = {}
        self.progress = ProgressTracker(database)
        self.scheduling = TaskScheduler(
            database, self.scheduler, self._scheduled_run, self._publish_task_updated
        )
        self.tasks = TaskService(
            database,
            self._task_revisions,
            self.running,
            self._sync_schedule,
            self._publish_task_updated,
        )
        self.workflows = WorkflowService(
            database, self._bump_task_revision, self._sync_schedule, self._publish_task_updated
        )

    def next_task_event_id(self) -> int:
        return self.progress.next_task_event_id()

    def subscribe_task(self, task_id: str) -> asyncio.Queue[int]:
        return self.progress.subscribe_task(task_id)

    def unsubscribe_task(self, task_id: str, queue: asyncio.Queue[int]) -> None:
        return self.progress.unsubscribe_task(task_id, queue)

    def _publish_task_updated(self, task_id: str) -> None:
        return self.progress._publish_task_updated(task_id)

    def get_task_run_progress(self, task_id: str) -> dict[str, Any] | None:
        return self.progress.get_task_run_progress(task_id)

    @staticmethod
    def _progress_step_statuses(progress: dict[str, object]) -> list[dict[str, object]]:
        return ProgressTracker._progress_step_statuses(progress)

    @classmethod
    def _workflow_step_statuses(
        cls, steps: list[dict[str, object]], path_prefix: str = "steps", *, top_level: bool = True
    ) -> list[dict[str, object]]:
        return ProgressTracker._workflow_step_statuses(steps, path_prefix, top_level=top_level)

    def _initialize_run_progress(
        self, task: Task, run: TaskRun, *, workflow_snapshot: dict[str, object] | None = None
    ) -> None:
        return self.progress._initialize_run_progress(
            task, run, workflow_snapshot=workflow_snapshot
        )

    def _persist_run_progress(self, task_id: str, run_id: str) -> None:
        return self.progress._persist_run_progress(task_id, run_id)

    def _append_run_log(
        self,
        task_id: str,
        run_id: str,
        message: str,
        *,
        level: str = "INFO",
        step_index: int | None = None,
        node_id: str | None = None,
        step_path: str | None = None,
    ) -> None:
        return self.progress._append_run_log(
            task_id,
            run_id,
            message,
            level=level,
            step_index=step_index,
            node_id=node_id,
            step_path=step_path,
        )

    def _begin_run_attempt(self, task_id: str, run_id: str, attempt: int) -> None:
        return self.progress._begin_run_attempt(task_id, run_id, attempt)

    def _update_run_step(
        self,
        task_id: str,
        run_id: str,
        index: int | None,
        status: str,
        error: str | None = None,
        bot_response: str | None = None,
        bot_buttons: list[list[str]] | None = None,
        duration_ms: int | None = None,
        node_id: str | None = None,
        step_path: str | None = None,
        selected_branch: dict[str, object] | None = None,
        condition_variables: list[dict[str, object]] | None = None,
        include_condition_values: bool = False,
    ) -> None:
        return self.progress._update_run_step(
            task_id,
            run_id,
            index,
            status,
            error,
            bot_response,
            bot_buttons,
            duration_ms,
            node_id,
            step_path,
            selected_branch,
            condition_variables,
            include_condition_values,
        )

    def _finalize_run_progress(
        self, task_id: str, run_id: str, status: str, error: str | None = None
    ) -> None:
        return self.progress._finalize_run_progress(task_id, run_id, status, error)

    async def start(self) -> None:
        self.settings.ensure_directories()
        self.database.create_all()
        self.scheduler.start()
        for task in self.database.list_tasks():
            if (
                task.enabled
                and not task.archived
                and self.database.get_latest_workflow_version(task.id) is not None
            ):
                self._ensure_next_run(task)
                self._schedule_task(task)

    async def close(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        active = list(set(self.running.values()))
        for running in active:
            running.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
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
            logger.info("🚀 签到服务已启动")
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
        return self.scheduling._ensure_next_run(task)

    def _schedule_task(self, task: Task) -> None:
        return self.scheduling._schedule_task(task)

    def _remove_scheduled_task(self, task_id: str) -> None:
        return self.scheduling._remove_scheduled_task(task_id)

    def _sync_schedule(self, task: Task) -> None:
        return self.scheduling._sync_schedule(task)

    def _bump_task_revision(self, task_id: str) -> None:
        self._task_revisions[task_id] = self._task_revisions.get(task_id, 0) + 1

    @staticmethod
    def _task_from_definition(account: Account, definition: TaskDefinition) -> Task:
        return TaskService._task_from_definition(account, definition)

    def create_task(self, definition: TaskDefinition) -> Task:
        return self.tasks.create_task(definition)

    def edit_task(self, task_id: str, definition: TaskDefinition) -> Task:
        return self.tasks.edit_task(task_id, definition)

    @staticmethod
    def _execution_definition(definition: TaskDefinition) -> dict[str, object]:
        return WorkflowService._execution_definition(definition)

    def publish_task(self, task_id: str, release_note: str | None = None) -> WorkflowVersion:
        return self.workflows.publish_task(task_id, release_note)

    def workflow_versions(self, task_id: str) -> list[WorkflowVersion]:
        return self.workflows.workflow_versions(task_id)

    def enable_task(self, task_id: str) -> Task:
        return self.tasks.enable_task(task_id)

    def disable_task(self, task_id: str) -> Task:
        return self.tasks.disable_task(task_id)

    def skip_next_task(self, task_id: str) -> Task:
        return self.tasks.skip_next_task(task_id)

    def archive_task(self, task_id: str) -> Task:
        return self.tasks.archive_task(task_id)

    def restore_task(self, task_id: str) -> Task:
        return self.tasks.restore_task(task_id)

    async def _scheduled_run(self, task_id: str, planned_at: datetime | None = None) -> None:
        if planned_at is not None:
            current = self.database.get_task(task_id)
            if current is None or current.next_run_at != planned_at:
                logger.info("忽略过期调度回调 task_id=%s", task_id)
                return
        await self.run_task(task_id)

    async def _watch_cancellation(self, task_id: str, running: asyncio.Task[bool]) -> None:
        while True:
            await asyncio.sleep(1)
            task = self.database.get_task(task_id)
            if task is None:
                return
            if task.cancel_requested:
                if not running.cancelling():
                    running.cancel()
                return

    def _execution_snapshot(
        self,
        task: Task,
        execution_definition: dict[str, object],
        *,
        task_name: str | None = None,
    ) -> tuple[Task, Account]:
        """Build an isolated Task object for one published/test execution."""

        account_name = execution_definition.get("account")
        if not isinstance(account_name, str):
            raise AccountNotFoundError("任务绑定的账号不存在")
        account = self.database.get_account(account_name)
        if account is None:
            raise AccountNotFoundError("任务绑定的账号不存在")
        target = execution_definition.get("target")
        if not isinstance(target, str) or not target:
            raise TaskStateError("工作流版本缺少执行目标")
        merged = task.config.copy()
        merged.update(execution_definition)
        # Schedule is intentionally task-level.  It controls when a run is
        # created, not the behavior of an already-created workflow version.
        # Published runs receive the active task schedule.  Test runs pass a
        # complete unsaved definition and must retain that draft schedule.
        if not isinstance(execution_definition.get("schedule"), dict):
            merged["schedule"] = schedule_from_task(task).model_dump(mode="json")
        merged["name"] = task_name or task.name
        snapshot = copy.copy(task)
        snapshot.account_id = account.id
        snapshot.target = target
        snapshot.name = task_name or task.name
        snapshot.config_json = json.dumps(merged, ensure_ascii=False)
        return snapshot, account

    @staticmethod
    def _log_bot_response_enabled(task: Task) -> bool:
        return bool(task.config.get("log_bot_response", False))

    @staticmethod
    def _log_condition_values_enabled(task: Task) -> bool:
        return bool(task.config.get("log_condition_values", False))

    async def _record_skipped(
        self,
        task: Task,
        execution_task: Task,
        *,
        version: WorkflowVersion,
    ) -> None:
        finished = utc_now()
        run = self.database.add_run(
            TaskRun(
                task_id=task.id,
                planned_at=task.next_run_at,
                run_kind="published",
                workflow_version=str(version.version_number),
                workflow_version_id=version.id,
            )
        )
        self._initialize_run_progress(execution_task, run)
        self.database.update_run(
            run.id,
            finished_at=finished,
            status="skipped",
            attempts=0,
            error="目标聊天忙碌",
        )
        self._finalize_run_progress(task.id, run.id, "skipped", "目标聊天忙碌")
        next_run = self._update_after_run(
            task,
            "skipped",
            finished,
            manual=False,
            revision=self._task_revisions.get(task.id, 0),
        )
        await self.notifications.skipped(task, next_run)

    @staticmethod
    def _definition_unchanged(snapshot: Task, current: Task) -> bool:
        return (
            snapshot.account_id == current.account_id
            and snapshot.config_json == current.config_json
        )

    def _update_after_run(
        self,
        snapshot: Task,
        status: str,
        finished: datetime,
        *,
        manual: bool,
        revision: int,
    ) -> datetime | None:
        """Update history fields without letting an old run undo newer edits.

        Execution uses ``snapshot`` throughout.  If an administrator edited,
        disabled or archived the task while it was running, the edit method
        has already synchronized the future scheduler state and that state is
        preserved here.
        """

        current = self.database.get_task_any(snapshot.id)
        if current is None:
            return None
        values: dict[str, object] = {"last_run_at": finished, "last_status": status}
        if (
            not manual
            and current.enabled
            and not current.archived
            and self._task_revisions.get(snapshot.id, 0) == revision
            and self._definition_unchanged(snapshot, current)
        ):
            try:
                # The run may finish before its planned wall-clock time (for
                # example after a clock adjustment or a delayed scheduler
                # callback).  Exclude the occurrence that was just consumed,
                # not only the completion timestamp, so a daily task cannot
                # be scheduled again later on the same day.
                values["next_run_at"] = next_run_for(
                    schedule_from_task(current),
                    now=finished,
                    after=current.next_run_at,
                )
            except ValueError:
                values["next_run_at"] = None
        updated = self.database.update_task(current.id, **values)
        self._sync_schedule(updated)
        self._publish_task_updated(current.id)
        return updated.next_run_at

    async def _execute_run(
        self,
        task: Task,
        account: Account,
        run: TaskRun,
        *,
        manual: bool,
        update_task_state: bool = True,
        state_task: Task | None = None,
    ) -> bool:
        state_snapshot = state_task or task
        lock_key = (account.id, task.target)
        lock = self.locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            revision = self._task_revisions.get(task.id, 0)
            current = self.database.get_task_any(task.id)
            if current is not None and current.cancel_requested:
                self.database.update_task(task.id, cancel_requested=False)
            running = asyncio.current_task()
            if running is None:
                raise RuntimeError("无法获取当前任务实例")
            self.running[task.id] = running
            self._publish_task_updated(task.id)
            cancel_watcher = asyncio.create_task(self._watch_cancellation(task.id, running))
            bot_response: str | None = None
            client_acquired = False
            try:
                client = await self.pool.acquire(account)
                client_acquired = True

                def is_cancelled() -> bool:
                    current = self.database.get_task_any(task.id)
                    return bool(current and current.cancel_requested)

                async def on_attempt(attempt: int) -> None:
                    self._begin_run_attempt(task.id, run.id, attempt)

                async def on_step_status(
                    index: int | None,
                    status: str,
                    error: str | None,
                    duration_ms: int | None = None,
                    node_id: str | None = None,
                    step_path: str | None = None,
                    selected_branch: dict[str, object] | None = None,
                    condition_variables: list[dict[str, object]] | None = None,
                ) -> None:
                    self._update_run_step(
                        task.id,
                        run.id,
                        index,
                        status,
                        error,
                        duration_ms=duration_ms,
                        node_id=node_id,
                        step_path=step_path,
                        selected_branch=selected_branch,
                        condition_variables=condition_variables,
                        include_condition_values=self._log_condition_values_enabled(task),
                    )

                async def on_step_response(
                    index: int | None,
                    response: str,
                    buttons: list[list[str]] | None = None,
                    node_id: str | None = None,
                    step_path: str | None = None,
                ) -> None:
                    self._update_run_step(
                        task.id,
                        run.id,
                        index,
                        "running",
                        bot_response=response,
                        bot_buttons=buttons,
                        node_id=node_id,
                        step_path=step_path,
                    )

                success, error, attempts, bot_response = await run_with_retries(
                    CheckinExecutor(
                        client,
                        is_cancelled=is_cancelled,
                        on_attempt=on_attempt,
                        on_step_status=on_step_status,
                        on_step_response=on_step_response,
                    ),
                    task,
                )
                finished = utc_now()
                status = "success" if success else "failed"
                self.database.update_run(
                    run.id,
                    finished_at=finished,
                    status=status,
                    attempts=attempts,
                    error=error,
                )
                self._finalize_run_progress(task.id, run.id, status, error)
                if update_task_state:
                    next_run = self._update_after_run(
                        state_snapshot, status, finished, manual=manual, revision=revision
                    )
                else:
                    current = self.database.get_task_any(task.id)
                    next_run = current.next_run_at if current else None
                if self._log_bot_response_enabled(task) and bot_response is not None:
                    logger.info(
                        "🤖 机器人回复 task_id=%s name=%s status=%s\n%s",
                        task.id,
                        task.name,
                        status,
                        bot_response,
                    )
                if success:
                    await self.notifications.success(task, next_run, bot_response)
                else:
                    await self.notifications.failure(
                        task, error or "未知错误", next_run, bot_response
                    )
                return success
            except asyncio.CancelledError:
                finished = utc_now()
                current = self.database.get_task_any(task.id)
                requested = bool(current and current.cancel_requested)
                reason = "收到取消请求" if requested else "执行被中断"
                self.database.update_run(
                    run.id, finished_at=finished, status="canceled", error=reason
                )
                self._finalize_run_progress(task.id, run.id, "canceled", reason)
                if update_task_state:
                    next_run = self._update_after_run(
                        state_snapshot, "canceled", finished, manual=manual, revision=revision
                    )
                else:
                    current = self.database.get_task_any(task.id)
                    next_run = current.next_run_at if current else None
                await self.notifications.canceled(task, next_run, reason)
                if not requested:
                    raise
                return False
            except Exception as exc:
                finished = utc_now()
                self.database.update_run(
                    run.id, finished_at=finished, status="failed", error=str(exc)
                )
                self._finalize_run_progress(task.id, run.id, "failed", str(exc))
                if update_task_state:
                    next_run = self._update_after_run(
                        state_snapshot, "failed", finished, manual=manual, revision=revision
                    )
                else:
                    current = self.database.get_task_any(task.id)
                    next_run = current.next_run_at if current else None
                await self.notifications.failure(task, str(exc), next_run, bot_response)
                return False
            finally:
                if client_acquired:
                    await self.pool.release(account)
                cancel_watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await cancel_watcher
                current = self.database.get_task_any(task.id)
                if current is not None and current.cancel_requested:
                    self.database.update_task(task.id, cancel_requested=False)
                if self.running.get(task.id) is running:
                    self.running.pop(task.id, None)
                self._publish_task_updated(task.id)

    async def run_task(self, task_id: str) -> bool:
        task = self.database.get_task(task_id)
        if not task or task.archived or not task.enabled:
            raise RuntimeError("任务不存在、已归档或未启用")
        version = self.database.get_latest_workflow_version(task.id)
        if version is None:
            raise WorkflowVersionNotFound("请先发布工作流后再运行任务")
        execution_task, account = self._execution_snapshot(task, version.execution_definition)
        lock_key = (account.id, execution_task.target)
        lock = self.locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked() or lock_key in self._manual_reservations:
            logger.warning("⚠️ 任务 %s 因目标聊天忙碌而跳过本次执行", task.name)
            await self._record_skipped(task, execution_task, version=version)
            return False
        run = self.database.add_run(
            TaskRun(
                task_id=task.id,
                planned_at=task.next_run_at,
                run_kind="published",
                workflow_version=str(version.version_number),
                workflow_version_id=version.id,
            )
        )
        self._initialize_run_progress(execution_task, run)
        return await self._execute_run(
            execution_task,
            account,
            run,
            manual=False,
            state_task=task,
        )

    def start_manual_run(self, task_id: str) -> str:
        """Start a manual execution in the background and return its run row.

        Manual executions are allowed for disabled tasks, do not move the
        configured future schedule, and fail before inserting history when the
        account/target serialization key is already occupied.
        """

        task = self.database.get_task_any(task_id)
        if task is None:
            raise TaskNotFound("任务不存在")
        if task.archived:
            raise TaskStateError("归档任务不能手动运行")
        version = self.database.get_latest_workflow_version(task.id)
        if version is None:
            raise WorkflowVersionNotFound("请先发布工作流后再运行任务")
        execution_task, account = self._execution_snapshot(task, version.execution_definition)
        lock_key = (account.id, execution_task.target)
        lock = self.locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked() or lock_key in self._manual_reservations or task.id in self.running:
            raise ManualRunConflict("同一账号和目标当前已有运行实例")

        self.database.update_task(task.id, cancel_requested=False)
        run = self.database.add_run(
            TaskRun(
                task_id=task.id,
                planned_at=None,
                run_kind="published",
                workflow_version=str(version.version_number),
                workflow_version_id=version.id,
            )
        )
        return self._start_background_run(task, execution_task, account, run, lock_key)

    def start_test_run(self, task_id: str, definition: TaskDefinition) -> str:
        """Start a test run from the editor's current (possibly unsaved) state."""

        task = self.database.get_task_any(task_id)
        if task is None:
            raise TaskNotFound("任务不存在")
        if task.archived:
            raise TaskStateError("归档任务不能测试")
        execution_definition = definition.model_dump(mode="json")
        execution_task, account = self._execution_snapshot(
            task, execution_definition, task_name=definition.name
        )
        lock_key = (account.id, execution_task.target)
        lock = self.locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked() or lock_key in self._manual_reservations or task.id in self.running:
            raise ManualRunConflict("同一账号和目标当前已有运行实例")

        run = self.database.add_run(
            TaskRun(
                task_id=task.id,
                planned_at=None,
                run_kind="test",
                workflow_version="main",
                workflow_version_id=None,
            )
        )
        return self._start_background_run(
            task,
            execution_task,
            account,
            run,
            lock_key,
            workflow_snapshot=execution_definition,
            update_task_state=False,
        )

    def _start_background_run(
        self,
        task: Task,
        execution_task: Task,
        account: Account,
        run: TaskRun,
        lock_key: tuple[str, str],
        *,
        workflow_snapshot: dict[str, Any] | None = None,
        update_task_state: bool = True,
    ) -> str:
        self._initialize_run_progress(execution_task, run, workflow_snapshot=workflow_snapshot)
        self._manual_reservations.add(lock_key)
        revision = self._task_revisions.get(task.id, 0)

        async def execute() -> bool:
            try:
                return await self._execute_run(
                    execution_task,
                    account,
                    run,
                    manual=True,
                    update_task_state=update_task_state,
                    state_task=task,
                )
            finally:
                self._manual_reservations.discard(lock_key)

        running = asyncio.create_task(execute(), name=f"{run.run_kind}:{task.id}:{run.id}")
        self.running[task.id] = running
        self._publish_task_updated(task.id)

        def cleanup(completed: asyncio.Task[bool]) -> None:
            self._manual_reservations.discard(lock_key)
            if self.running.get(task.id) is completed:
                self.running.pop(task.id, None)
            if not completed.cancelled():
                completed.exception()
            stored = self.database.get_run(run.id)
            if stored is not None and stored.status == "running":
                status = "canceled" if completed.cancelled() else "failed"
                finished = utc_now()
                error = "执行在启动前被取消" if completed.cancelled() else "执行异常中止"
                self.database.update_run(
                    run.id,
                    finished_at=finished,
                    status=status,
                    error=error,
                )
                self._finalize_run_progress(task.id, run.id, status, error)
                if update_task_state:
                    self._update_after_run(
                        task,
                        status,
                        finished,
                        manual=True,
                        revision=revision,
                    )
            self._publish_task_updated(task.id)

        running.add_done_callback(cleanup)
        return run.id

    async def cancel_task(self, task_id: str) -> bool:
        task = self.database.get_task_any(task_id)
        if task is None:
            return False
        running = self.running.get(task.id)
        if running is None and not self.database.has_running_run(task.id):
            return False
        self.database.update_task(task.id, cancel_requested=True)
        self._publish_task_updated(task.id)
        if running is not None:
            running.cancel()
        await self.notifications.cancel_requested(task)
        return True
