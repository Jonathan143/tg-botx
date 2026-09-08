from __future__ import annotations

import json
import logging
from zoneinfo import ZoneInfo

from tg_botx.features.checkin.errors import AccountNotFoundError as AccountNotFoundError
from tg_botx.features.checkin.errors import ManualRunConflict as ManualRunConflict
from tg_botx.features.checkin.errors import TaskNameConflictError as TaskNameConflictError
from tg_botx.features.checkin.errors import TaskNotFound as TaskNotFound
from tg_botx.features.checkin.errors import TaskStateError as TaskStateError
from tg_botx.features.checkin.errors import WorkflowVersionNotFound as WorkflowVersionNotFound
from tg_botx.features.checkin.notifications import NotificationService as NotificationService
from tg_botx.features.checkin.schedule import next_run_for, schedule_from_task
from tg_botx.infrastructure.persistence.db import (
    Account,
    Task,
    TaskRun,
    utc_isoformat,
    utc_now,
)
from tg_botx.integrations.client_pool import ClientPool as ClientPool
from tg_botx.schemas import TaskDefinition

logger = logging.getLogger(__name__)


class TaskService:
    def __init__(self, database, revisions, running, sync_schedule, publish):
        self.database = database
        self._task_revisions = revisions
        self.running = running
        self._sync_schedule = sync_schedule
        self._publish_task_updated = publish

    def _bump_task_revision(self, task_id: str) -> None:
        self._task_revisions[task_id] = self._task_revisions.get(task_id, 0) + 1

    @staticmethod
    def _task_from_definition(account: Account, definition: TaskDefinition) -> Task:
        schedule = definition.schedule
        if schedule.start_date is None:
            local_today = utc_now().astimezone(ZoneInfo(schedule.timezone)).date()
            schedule = schedule.model_copy(update={"start_date": local_today})
            definition = definition.model_copy(update={"schedule": schedule})
        return Task(
            account_id=account.id,
            name=definition.name,
            target=definition.target,
            timezone=schedule.timezone,
            schedule_type=schedule.type,
            fixed_time=schedule.time,
            random_start=schedule.start,
            random_end=schedule.end,
            config_json=json.dumps(definition.model_dump(mode="json"), ensure_ascii=False),
            published_schedule_json=None,
            enabled=False,
            next_run_at=None,
        )

    def create_task(self, definition: TaskDefinition) -> Task:
        if self.database.get_task_any(definition.name) is not None:
            raise TaskNameConflictError("任务名称已存在")
        account = self.database.get_account(definition.account)
        if account is None:
            raise AccountNotFoundError("任务绑定的账号不存在")
        task = self.database.save_task(self._task_from_definition(account, definition))
        self._publish_task_updated(task.id)
        return task

    def edit_task(self, task_id: str, definition: TaskDefinition) -> Task:
        task = self.database.get_task_any(task_id)
        if task is None:
            raise TaskNotFound("任务不存在")
        if task.archived:
            raise TaskStateError("归档任务需先恢复后才能编辑")
        duplicate = self.database.get_task_any(definition.name)
        if duplicate is not None and duplicate.id != task.id:
            raise TaskNameConflictError("任务名称已存在")
        account = self.database.get_account(definition.account)
        if account is None:
            raise AccountNotFoundError("任务绑定的账号不存在")

        schedule = definition.schedule
        if schedule.start_date is None:
            local_today = utc_now().astimezone(ZoneInfo(schedule.timezone)).date()
            schedule = schedule.model_copy(update={"start_date": local_today})
            definition = definition.model_copy(update={"schedule": schedule})
        next_run = None
        values: dict[str, object] = {
            "account_id": account.id,
            "name": definition.name,
            "target": definition.target,
            "config_json": json.dumps(definition.model_dump(mode="json"), ensure_ascii=False),
        }
        # An enabled task keeps its currently published schedule and pending
        # occurrence while this edit remains a draft.  Disabled tasks have no
        # active scheduler job, so their denormalized fields may follow the
        # draft; the published snapshot still becomes authoritative on
        # publish.
        if task.enabled:
            values["next_run_at"] = task.next_run_at
        else:
            values.update(
                {
                    "timezone": schedule.timezone,
                    "schedule_type": schedule.type,
                    "fixed_time": schedule.time,
                    "random_start": schedule.start,
                    "random_end": schedule.end,
                    "next_run_at": next_run,
                }
            )
        updated = self.database.update_task(task.id, **values)
        self._bump_task_revision(task.id)
        if not task.enabled:
            self._sync_schedule(updated)
        self._publish_task_updated(task.id)
        return updated

    def enable_task(self, task_id: str) -> Task:
        task = self.database.get_task_any(task_id)
        if task is None:
            raise TaskNotFound("任务不存在")
        if task.archived:
            raise TaskStateError("归档任务需先恢复后才能启用")
        if self.database.get_latest_workflow_version(task.id) is None:
            raise TaskStateError("请先发布工作流后再启用任务")
        try:
            next_run = next_run_for(schedule_from_task(task), now=utc_now())
        except ValueError as exc:
            raise TaskStateError("调度规则没有可执行的未来时间") from exc
        updated = self.database.update_task(task.id, enabled=True, next_run_at=next_run)
        self._bump_task_revision(task.id)
        self._sync_schedule(updated)
        self._publish_task_updated(task.id)
        return updated

    def disable_task(self, task_id: str) -> Task:
        task = self.database.get_task_any(task_id)
        if task is None:
            raise TaskNotFound("任务不存在")
        updated = self.database.update_task(task.id, enabled=False, next_run_at=None)
        self._bump_task_revision(task.id)
        self._sync_schedule(updated)
        self._publish_task_updated(task.id)
        return updated

    def skip_next_task(self, task_id: str) -> Task:
        """Skip the currently scheduled occurrence and advance the schedule.

        Skipping is an administrator action on the future schedule only; it
        does not affect an execution that is already running.  The consumed
        occurrence is recorded as a skipped run so task history and dashboard
        counters retain an auditable record of the action.
        """

        task = self.database.get_task_any(task_id)
        if task is None:
            raise TaskNotFound("任务不存在")
        if task.archived:
            raise TaskStateError("归档任务不能跳过下次运行")
        if not task.enabled:
            raise TaskStateError("任务未启用，无法跳过下次运行")
        planned_at = task.next_run_at
        if planned_at is None:
            raise TaskStateError("任务当前没有安排中的下次运行")

        finished = utc_now()
        # A scheduled callback keeps the consumed occurrence in
        # ``next_run_at`` until it finishes.  Do not create a second skipped
        # history row if that occurrence has already started.
        if planned_at <= finished and (
            task.id in self.running or self.database.has_running_run(task.id)
        ):
            raise TaskStateError("任务当前正在执行，无法跳过已开始的运行")
        try:
            next_run = next_run_for(
                schedule_from_task(task),
                now=finished,
                after=planned_at,
            )
        except ValueError:
            next_run = None

        version = self.database.get_latest_workflow_version(task.id)
        skipped_run = self.database.add_run(
            TaskRun(
                task_id=task.id,
                planned_at=planned_at,
                started_at=finished,
                finished_at=finished,
                status="skipped",
                attempts=0,
                error="管理员跳过本次运行",
                run_kind="published",
                workflow_version=str(version.version_number) if version else None,
                workflow_version_id=version.id if version else None,
            )
        )
        updated = self.database.update_task(
            task.id,
            next_run_at=next_run,
            last_run_at=finished,
            last_status="skipped",
        )
        self._bump_task_revision(task.id)
        self._sync_schedule(updated)
        self._publish_task_updated(task.id)
        logger.info(
            "跳过任务下次运行 task_id=%s name=%s planned_at=%s next_run_at=%s run_id=%s",
            task.id,
            task.name,
            utc_isoformat(planned_at),
            utc_isoformat(next_run),
            skipped_run.id,
        )
        return updated

    def archive_task(self, task_id: str) -> Task:
        task = self.database.get_task_any(task_id)
        if task is None:
            raise TaskNotFound("任务不存在")
        # Archiving always disables future scheduling, but deliberately leaves
        # an already running execution untouched.
        updated = self.database.update_task(task.id, enabled=False, archived=True, next_run_at=None)
        self._bump_task_revision(task.id)
        self._sync_schedule(updated)
        self._publish_task_updated(task.id)
        return updated

    def restore_task(self, task_id: str) -> Task:
        task = self.database.get_task_any(task_id)
        if task is None:
            raise TaskNotFound("任务不存在")
        updated = self.database.update_task(
            task.id, archived=False, enabled=False, next_run_at=None
        )
        self._bump_task_revision(task.id)
        self._sync_schedule(updated)
        self._publish_task_updated(task.id)
        return updated
