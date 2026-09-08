from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    func,
    select,
)

from tg_botx.infrastructure.persistence.models import (
    Task,
    TaskRun,
)


class DashboardRepository:
    def __init__(self, session_factory):
        self.session = session_factory

    def dashboard_stats(self, since: datetime) -> dict[str, Any]:
        """Return compact aggregate counters used by the administration dashboard."""

        with self.session() as session:
            task_rows = session.execute(
                select(Task.enabled, Task.archived, func.count(Task.id)).group_by(
                    Task.enabled, Task.archived
                )
            )
            result: dict[str, Any] = {
                "tasks_total": 0,
                "tasks_enabled": 0,
                "tasks_archived": 0,
                "runs_total": 0,
                "runs_success": 0,
                "runs_failed": 0,
                "runs_canceled": 0,
                "runs_skipped": 0,
                "runs_running": 0,
                "runStatusCounts": {},
            }
            for enabled, archived, count in task_rows:
                value = int(count)
                result["tasks_total"] += value
                if enabled and not archived:
                    result["tasks_enabled"] += value
                if archived:
                    result["tasks_archived"] += value

            run_rows = session.execute(
                select(TaskRun.status, func.count(TaskRun.id))
                .where(TaskRun.started_at >= since)
                .group_by(TaskRun.status)
            )
            for status, count in run_rows:
                value = int(count)
                result["runs_total"] += value
                result["runStatusCounts"][status] = value
                key = f"runs_{status}"
                if key in result:
                    result[key] += value
            return result

    def dashboard_run_events(self, since: datetime) -> list[tuple[datetime, str]]:
        """Return the minimal run data needed to build UTC dashboard buckets."""

        with self.session() as session:
            rows = session.execute(
                select(TaskRun.started_at, TaskRun.status)
                .where(TaskRun.started_at >= since)
                .order_by(TaskRun.started_at)
            )
            return [(started_at, status) for started_at, status in rows]

    def upcoming_tasks(self, limit: int = 10) -> list[Task]:
        with self.session() as session:
            query = (
                select(Task)
                .where(
                    Task.enabled.is_(True),
                    Task.archived.is_(False),
                    Task.next_run_at.is_not(None),
                )
                .order_by(Task.next_run_at, Task.id)
                .limit(min(max(limit, 1), 100))
            )
            return list(session.scalars(query))
