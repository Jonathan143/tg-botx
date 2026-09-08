from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from tg_botx.config import Settings
from tg_botx.core.ports import Clock
from tg_botx.core.time import SystemClock
from tg_botx.features.checkin.coordinator import RunCoordinator
from tg_botx.features.checkin.errors import AccountNotFoundError as AccountNotFoundError
from tg_botx.features.checkin.errors import ManualRunConflict as ManualRunConflict
from tg_botx.features.checkin.errors import TaskNameConflictError as TaskNameConflictError
from tg_botx.features.checkin.errors import TaskNotFound as TaskNotFound
from tg_botx.features.checkin.errors import TaskStateError as TaskStateError
from tg_botx.features.checkin.errors import WorkflowVersionNotFound as WorkflowVersionNotFound
from tg_botx.features.checkin.executor import run_with_retries
from tg_botx.features.checkin.notifications import NotificationService as NotificationService
from tg_botx.features.checkin.progress import ProgressTracker
from tg_botx.features.checkin.scheduler import TaskScheduler
from tg_botx.features.checkin.tasks import TaskService
from tg_botx.features.checkin.workflows import WorkflowService
from tg_botx.infrastructure.persistence.db import (
    Account,
    Database,
    Task,
    TaskRun,
    WorkflowVersion,
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
        clock: Clock | None = None,
    ):
        self.clock = clock if clock is not None else SystemClock()
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
            database, self.scheduler, self._scheduled_run, self._publish_task_updated, self.clock
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

        self.coordinator = RunCoordinator(
            database,
            self.pool,
            self.notifications,
            self.progress,
            self.scheduling,
            self.clock,
            self.running,
            self.locks,
            self._manual_reservations,
            self._task_revisions,
            lambda executor, task: run_with_retries(executor, task),
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
        try:
            await self.pool.close()
        finally:
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
        return await self.coordinator._scheduled_run(task_id, planned_at)

    async def _watch_cancellation(self, task_id: str, running: asyncio.Task[bool]) -> None:
        return await self.coordinator._watch_cancellation(task_id, running)

    def _execution_snapshot(
        self, task: Task, execution_definition: dict[str, object], *, task_name: str | None = None
    ) -> tuple[Task, Account]:
        return self.coordinator._execution_snapshot(task, execution_definition, task_name=task_name)

    @staticmethod
    def _log_bot_response_enabled(task: Task) -> bool:
        return RunCoordinator._log_bot_response_enabled(task)

    @staticmethod
    def _log_condition_values_enabled(task: Task) -> bool:
        return RunCoordinator._log_condition_values_enabled(task)

    async def _record_skipped(
        self, task: Task, execution_task: Task, *, version: WorkflowVersion
    ) -> None:
        return await self.coordinator._record_skipped(task, execution_task, version=version)

    @staticmethod
    def _definition_unchanged(snapshot: Task, current: Task) -> bool:
        return RunCoordinator._definition_unchanged(snapshot, current)

    def _update_after_run(
        self, snapshot: Task, status: str, finished: datetime, *, manual: bool, revision: int
    ) -> datetime | None:
        return self.coordinator._update_after_run(
            snapshot, status, finished, manual=manual, revision=revision
        )

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
        return await self.coordinator._execute_run(
            task,
            account,
            run,
            manual=manual,
            update_task_state=update_task_state,
            state_task=state_task,
        )

    async def run_task(self, task_id: str) -> bool:
        return await self.coordinator.run_task(task_id)

    def start_manual_run(self, task_id: str) -> str:
        return self.coordinator.start_manual_run(task_id)

    def start_test_run(self, task_id: str, definition: TaskDefinition) -> str:
        return self.coordinator.start_test_run(task_id, definition)

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
        return self.coordinator._start_background_run(
            task,
            execution_task,
            account,
            run,
            lock_key,
            workflow_snapshot=workflow_snapshot,
            update_task_state=update_task_state,
        )

    async def cancel_task(self, task_id: str) -> bool:
        return await self.coordinator.cancel_task(task_id)
