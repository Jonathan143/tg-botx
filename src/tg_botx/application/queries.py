"""批量装配读模型，响应转换不再隐式逐条查询数据库。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tg_botx.features.checkin.runtime import CheckinService
from tg_botx.infrastructure.persistence.db import Account, Database, Task, WorkflowVersion


@dataclass(frozen=True, slots=True)
class TaskView:
    task: Task
    account: Account | None
    versions: list[WorkflowVersion]
    running: bool
    progress: dict[str, Any] | None


class TaskQueries:
    def __init__(self, database: Database, service: CheckinService):
        self.database = database
        self.service = service

    def views(self, tasks: list[Task]) -> list[TaskView]:
        accounts, versions, running = self.database.tasks.load_related(tasks)
        return [
            TaskView(
                task,
                accounts.get(task.account_id),
                versions.get(task.id, []),
                task.id in self.service.running or task.id in running,
                self.service.get_task_run_progress(task.id),
            )
            for task in tasks
        ]
