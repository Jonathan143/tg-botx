from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    func,
    select,
)

from tg_botx.core.time import utc_isoformat as utc_isoformat
from tg_botx.infrastructure.persistence.models import (
    PERMANENT_EXPIRY as PERMANENT_EXPIRY,
)
from tg_botx.infrastructure.persistence.models import (
    Account as Account,
)
from tg_botx.infrastructure.persistence.models import (
    AccountChat as AccountChat,
)
from tg_botx.infrastructure.persistence.models import (
    AdminSession as AdminSession,
)
from tg_botx.infrastructure.persistence.models import (
    Base as Base,
)
from tg_botx.infrastructure.persistence.models import (
    BotAuditLog as BotAuditLog,
)
from tg_botx.infrastructure.persistence.models import (
    BotBinding as BotBinding,
)
from tg_botx.infrastructure.persistence.models import (
    BotBindingBatch as BotBindingBatch,
)
from tg_botx.infrastructure.persistence.models import (
    BotBindingCode as BotBindingCode,
)
from tg_botx.infrastructure.persistence.models import (
    BotCommandConfig as BotCommandConfig,
)
from tg_botx.infrastructure.persistence.models import (
    BotSetting as BotSetting,
)
from tg_botx.infrastructure.persistence.models import (
    BotUserPoint as BotUserPoint,
)
from tg_botx.infrastructure.persistence.models import (
    SchemaVersion as SchemaVersion,
)
from tg_botx.infrastructure.persistence.models import (
    Task as Task,
)
from tg_botx.infrastructure.persistence.models import (
    TaskRun as TaskRun,
)
from tg_botx.infrastructure.persistence.models import (
    UTCDateTime as UTCDateTime,
)
from tg_botx.infrastructure.persistence.models import (
    WorkflowVersion as WorkflowVersion,
)
from tg_botx.infrastructure.persistence.models import (
    utc_now as utc_now,
)


class RunsRepository:
    def __init__(self, session_factory):
        self.session = session_factory

    def add_run(self, run: TaskRun) -> TaskRun:
        with self.session() as session:
            session.add(run)
            session.commit()
            session.refresh(run)
            return run

    def update_run(self, run_id: str, **values) -> TaskRun:
        with self.session() as session:
            run = session.get(TaskRun, run_id)
            if not run:
                raise KeyError(run_id)
            for key, value in values.items():
                setattr(run, key, value)
            session.commit()
            session.refresh(run)
            return run

    def get_run(self, run_id: str) -> TaskRun | None:
        with self.session() as session:
            return session.get(TaskRun, run_id)

    def has_running_run(self, task_id: str) -> bool:
        with self.session() as session:
            query = (
                select(TaskRun.id)
                .where(TaskRun.task_id == task_id, TaskRun.status == "running")
                .limit(1)
            )
            return session.scalar(query) is not None

    def task_history(self, task_id: str, limit: int = 20) -> list[TaskRun]:
        with self.session() as session:
            query = (
                select(TaskRun)
                .where(TaskRun.task_id == task_id)
                .order_by(TaskRun.started_at.desc())
                .limit(limit)
            )
            return list(session.scalars(query))

    def list_runs(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        task_id: str | None = None,
        status: str | None = None,
        started_from: datetime | None = None,
        started_to: datetime | None = None,
    ) -> tuple[list[TaskRun], int]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        filters = []
        if task_id:
            filters.append(TaskRun.task_id == task_id)
        if status:
            filters.append(TaskRun.status == status)
        if started_from:
            filters.append(TaskRun.started_at >= started_from)
        if started_to:
            filters.append(TaskRun.started_at <= started_to)

        with self.session() as session:
            total = session.scalar(select(func.count(TaskRun.id)).where(*filters)) or 0
            query = (
                select(TaskRun)
                .where(*filters)
                .order_by(TaskRun.started_at.desc(), TaskRun.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return list(session.scalars(query)), int(total)
