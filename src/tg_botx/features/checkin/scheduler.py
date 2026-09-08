from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.triggers.date import DateTrigger

from tg_botx.features.checkin.errors import AccountNotFoundError as AccountNotFoundError
from tg_botx.features.checkin.errors import ManualRunConflict as ManualRunConflict
from tg_botx.features.checkin.errors import TaskNameConflictError as TaskNameConflictError
from tg_botx.features.checkin.errors import TaskNotFound as TaskNotFound
from tg_botx.features.checkin.errors import TaskStateError as TaskStateError
from tg_botx.features.checkin.errors import WorkflowVersionNotFound as WorkflowVersionNotFound
from tg_botx.features.checkin.notifications import NotificationService as NotificationService
from tg_botx.features.checkin.schedule import next_run_for, schedule_from_task
from tg_botx.infrastructure.persistence.db import (
    Task,
    utc_isoformat,
)
from tg_botx.integrations.client_pool import ClientPool as ClientPool

logger = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(self, database, scheduler, scheduled_run, publish):
        self.database = database
        self.scheduler = scheduler
        self._scheduled_run = scheduled_run
        self._publish_task_updated = publish

    def _ensure_next_run(self, task: Task) -> None:
        now = datetime.now(UTC)
        if task.next_run_at is None or task.next_run_at <= now:
            try:
                next_run = next_run_for(schedule_from_task(task), now=now)
            except ValueError:
                self.database.update_task(task.id, next_run_at=None)
                self._remove_scheduled_task(task.id)
                return
            self.database.update_task(task.id, next_run_at=next_run)
            task.next_run_at = next_run
            self._publish_task_updated(task.id)

    def _schedule_task(self, task: Task) -> None:
        if task.next_run_at is None:
            return
        self.scheduler.add_job(
            self._scheduled_run,
            trigger=DateTrigger(run_date=task.next_run_at),
            # Carry the occurrence that created this one-shot job.  A stale
            # callback can still fire after an administrator advances the
            # schedule (for example via ``skip_next_task``); it must not run
            # the newly scheduled occurrence immediately.
            args=[task.id, task.next_run_at],
            id=f"task:{task.id}",
            # Use the configured task name instead of the callback name in
            # APScheduler's own "Added job" log entry.
            name=task.name,
            replace_existing=True,
            misfire_grace_time=None,
        )
        logger.info(
            "已安排任务 task_id=%s name=%s next_run_at=%s",
            task.id,
            task.name,
            utc_isoformat(task.next_run_at),
        )

    def _remove_scheduled_task(self, task_id: str) -> None:
        job = self.scheduler.get_job(f"task:{task_id}")
        if job is not None:
            self.scheduler.remove_job(job.id)

    def _sync_schedule(self, task: Task) -> None:
        if task.enabled and not task.archived and task.next_run_at is not None:
            self._schedule_task(task)
        else:
            self._remove_scheduled_task(task.id)
