from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from tg_botx.features.checkin.runtime import (
    CheckinService,
)
from tg_botx.infrastructure.persistence.db import (
    Database,
)
from tg_botx.interfaces.admin.errors import APIError
from tg_botx.interfaces.admin.presenters import _run_json

logger = logging.getLogger(__name__)


def build_router(database: Database, service: CheckinService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/runs")
    async def list_runs(
        page: int = Query(1, ge=1),
        page_size: int = Query(25, alias="pageSize", ge=1, le=100),
        task_id: str | None = Query(None, alias="taskId"),
        status: str | None = None,
        started_from: datetime | None = Query(None, alias="from"),
        started_to: datetime | None = Query(None, alias="to"),
    ) -> dict[str, Any]:
        items, total = database.list_runs(
            page=page,
            page_size=page_size,
            task_id=task_id,
            status=status,
            started_from=started_from,
            started_to=started_to,
        )
        task_lookup = database.tasks.get_many(list({item.task_id for item in items}))
        return {
            "items": [
                _run_json(
                    item,
                    database,
                    service,
                    include_workflow=False,
                    include_progress_logs=False,
                    task_lookup=task_lookup,
                )
                for item in items
            ],
            "page": page,
            "pageSize": page_size,
            "total": total,
        }

    @router.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = database.get_run(run_id)
        if not run:
            raise APIError("NOT_FOUND", "执行记录不存在", 404)
        return _run_json(run, database, service)

    return router
