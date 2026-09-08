from __future__ import annotations

import json

from tg_botx.features.checkin.errors import AccountNotFoundError as AccountNotFoundError
from tg_botx.features.checkin.errors import ManualRunConflict as ManualRunConflict
from tg_botx.features.checkin.errors import TaskNameConflictError as TaskNameConflictError
from tg_botx.features.checkin.errors import TaskNotFound as TaskNotFound
from tg_botx.features.checkin.errors import TaskStateError as TaskStateError
from tg_botx.features.checkin.errors import WorkflowVersionNotFound as WorkflowVersionNotFound
from tg_botx.features.checkin.notifications import NotificationService as NotificationService
from tg_botx.features.checkin.schedule import next_run_for
from tg_botx.infrastructure.persistence.db import (
    WorkflowVersion,
    utc_now,
)
from tg_botx.integrations.client_pool import ClientPool as ClientPool
from tg_botx.schemas import TaskDefinition


class WorkflowService:
    def __init__(self, database, bump_revision, sync_schedule, publish):
        self.database = database
        self._bump_task_revision = bump_revision
        self._sync_schedule = sync_schedule
        self._publish_task_updated = publish

    @staticmethod
    def _execution_definition(definition: TaskDefinition) -> dict[str, object]:
        """Return the immutable portion of a published workflow.

        Scheduling metadata is persisted separately as the task's published
        schedule.  All values consumed while executing steps or sending
        notifications are frozen here.
        """

        payload = definition.model_dump(mode="json")
        payload.pop("name", None)
        payload.pop("schedule", None)
        return payload

    def publish_task(self, task_id: str, release_note: str | None = None) -> WorkflowVersion:
        task = self.database.get_task_any(task_id)
        if task is None:
            raise TaskNotFound("任务不存在")
        if task.archived:
            raise TaskStateError("归档任务不能发布")
        try:
            definition = TaskDefinition.model_validate(task.config)
        except Exception as exc:
            raise TaskStateError("当前任务配置无效，无法发布") from exc
        schedule = definition.schedule
        if task.enabled:
            try:
                next_run = next_run_for(schedule, now=utc_now())
            except ValueError as exc:
                raise TaskStateError("调度规则没有可执行的未来时间") from exc
        else:
            next_run = None
        version = self.database.publish_workflow(
            task.id,
            self._execution_definition(definition),
            release_note=release_note,
            task_values={
                "timezone": schedule.timezone,
                "schedule_type": schedule.type,
                "fixed_time": schedule.time,
                "random_start": schedule.start,
                "random_end": schedule.end,
                "published_schedule_json": json.dumps(
                    schedule.model_dump(mode="json"), ensure_ascii=False
                ),
                "next_run_at": next_run,
            },
        )
        updated = self.database.get_task_any(task.id)
        if updated is None:
            raise TaskNotFound("任务不存在")
        self._bump_task_revision(task.id)
        self._sync_schedule(updated)
        self._publish_task_updated(task.id)
        return version

    def workflow_versions(self, task_id: str) -> list[WorkflowVersion]:
        return self.database.list_workflow_versions(task_id)
