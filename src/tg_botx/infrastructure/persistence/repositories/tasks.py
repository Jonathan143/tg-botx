from __future__ import annotations

import json
from typing import Any

from sqlalchemy import (
    func,
    or_,
    select,
)

from tg_botx.infrastructure.persistence.models import (
    Task,
    WorkflowVersion,
    utc_now,
)


class TasksRepository:
    def __init__(self, session_factory):
        self.session = session_factory

    def get_task(self, task_id_or_name: str) -> Task | None:
        with self.session() as session:
            task = session.get(Task, task_id_or_name)
            if task:
                return task
            return session.scalar(
                select(Task).where(Task.name == task_id_or_name, Task.archived.is_(False))
            )

    def get_task_any(self, task_id_or_name: str) -> Task | None:
        """Return a task by ID or name, including archived tasks."""

        with self.session() as session:
            task = session.get(Task, task_id_or_name)
            if task:
                return task
            return session.scalar(select(Task).where(Task.name == task_id_or_name))

    def list_tasks(self, include_archived: bool = False) -> list[Task]:
        with self.session() as session:
            query = select(Task).order_by(Task.created_at)
            if not include_archived:
                query = query.where(Task.archived.is_(False))
            return list(session.scalars(query))

    def list_tasks_page(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        include_archived: bool = False,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> tuple[list[Task], int]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        filters: list[Any] = []
        if not include_archived:
            filters.append(Task.archived.is_(False))
        if enabled is not None:
            filters.append(Task.enabled.is_(enabled))
        if search and (term := search.strip()):
            pattern = f"%{term}%"
            filters.append(or_(Task.name.ilike(pattern), Task.target.ilike(pattern)))

        with self.session() as session:
            total = session.scalar(select(func.count(Task.id)).where(*filters)) or 0
            query = (
                select(Task)
                .where(*filters)
                .order_by(Task.created_at.desc(), Task.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return list(session.scalars(query)), int(total)

    def save_task(self, task: Task) -> Task:
        with self.session() as session:
            session.add(task)
            session.commit()
            session.refresh(task)
            return task

    def update_task(self, task_id: str, **values) -> Task:
        with self.session() as session:
            task = session.get(Task, task_id)
            if not task:
                raise KeyError(task_id)
            for key, value in values.items():
                setattr(task, key, value)
            task.updated_at = utc_now()
            session.commit()
            session.refresh(task)
            return task

    def publish_workflow(
        self,
        task_id: str,
        execution_definition: dict[str, Any],
        *,
        release_note: str | None = None,
        published_by: str | None = None,
        task_values: dict[str, Any] | None = None,
    ) -> WorkflowVersion:
        with self.session() as session:
            task = session.get(Task, task_id, with_for_update=True)
            if task is None:
                raise KeyError(task_id)
            latest = session.scalar(
                select(WorkflowVersion.version_number)
                .where(WorkflowVersion.task_id == task_id)
                .order_by(WorkflowVersion.version_number.desc())
                .limit(1)
            )
            version = WorkflowVersion(
                task_id=task_id,
                version_number=(latest or 0) + 1,
                workflow_json=json.dumps(execution_definition, ensure_ascii=False),
                release_note=release_note.strip() if release_note else None,
                published_by=published_by,
            )
            if task_values is not None:
                for key, value in task_values.items():
                    setattr(task, key, value)
                task.updated_at = utc_now()
            session.add(version)
            session.commit()
            session.refresh(version)
            return version

    def get_workflow_version(self, version_id: str) -> WorkflowVersion | None:
        with self.session() as session:
            return session.get(WorkflowVersion, version_id)

    def get_latest_workflow_version(self, task_id: str) -> WorkflowVersion | None:
        with self.session() as session:
            return session.scalar(
                select(WorkflowVersion)
                .where(WorkflowVersion.task_id == task_id)
                .order_by(WorkflowVersion.version_number.desc())
                .limit(1)
            )

    def list_workflow_versions(self, task_id: str) -> list[WorkflowVersion]:
        with self.session() as session:
            return list(
                session.scalars(
                    select(WorkflowVersion)
                    .where(WorkflowVersion.task_id == task_id)
                    .order_by(WorkflowVersion.version_number.desc())
                )
            )
