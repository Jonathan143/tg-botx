from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query

from tg_botx.application.queries import TaskQueries
from tg_botx.features.checkin.runtime import (
    CheckinService,
)
from tg_botx.features.checkin.schedule import next_runs
from tg_botx.infrastructure.persistence.db import (
    Database,
    utc_now,
)
from tg_botx.interfaces.admin.errors import APIError
from tg_botx.interfaces.admin.models import EmptyBody, PublishBody, TaskBody
from tg_botx.interfaces.admin.presenters import _iso, _task_json, task_json

logger = logging.getLogger(__name__)


def build_router(database: Database, service: CheckinService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/tasks")
    async def list_tasks(
        page: int = Query(1, ge=1),
        page_size: int = Query(25, alias="pageSize", ge=1, le=100),
        include_archived: bool = Query(False, alias="includeArchived"),
        enabled: bool | None = None,
        search: str | None = Query(None, max_length=200),
    ) -> dict[str, Any]:
        items, total = database.list_tasks_page(
            page=page,
            page_size=page_size,
            include_archived=include_archived,
            enabled=enabled,
            search=search,
        )
        return {
            "items": [task_json(view) for view in TaskQueries(database, service).views(items)],
            "page": page,
            "pageSize": page_size,
            "total": total,
        }

    @router.post("/api/tasks", status_code=201)
    async def create_task(body: TaskBody) -> dict[str, Any]:
        if database.get_account(body.definition.account) is None:
            raise APIError("VALIDATION_FAILED", "Telegram 账号不存在", 422)
        task = service.create_task(body.definition)
        return _task_json(task, database, service)

    @router.post("/api/tasks/validate")
    async def validate_task(body: TaskBody) -> dict[str, Any]:
        if database.get_account(body.definition.account) is None:
            raise APIError(
                "VALIDATION_FAILED",
                "任务配置无效",
                422,
                details=[{"path": "definition.account", "message": "Telegram 账号不存在"}],
            )
        return {
            "valid": True,
            "definition": body.definition.to_api_dict(),
        }

    @router.post("/api/tasks/preview")
    async def preview_task(body: TaskBody) -> dict[str, Any]:
        schedule = body.definition.schedule
        runs = next_runs(schedule, now=utc_now(), count=5)
        return {
            "items": [_iso(item) for item in runs],
            "timezone": schedule.timezone,
        }

    @router.post("/api/tasks/{task_id}/preview")
    async def preview_existing_task(task_id: str, body: TaskBody) -> dict[str, Any]:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        schedule = body.definition.schedule
        start_after = task.next_run_at - timedelta(microseconds=1) if task.next_run_at else None
        runs = next_runs(schedule, now=utc_now(), count=5, start_after=start_after)
        return {
            "items": [_iso(item) for item in runs],
            "timezone": schedule.timezone,
        }

    @router.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        return _task_json(task, database, service)

    @router.get("/api/tasks/{task_id}/versions")
    async def list_workflow_versions(task_id: str) -> dict[str, Any]:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        versions = database.list_workflow_versions(task.id)
        return {
            "items": [
                {
                    "id": item.id,
                    "version": item.version_number,
                    "publishedAt": _iso(item.published_at),
                    "releaseNote": item.release_note,
                    "workflow": item.execution_definition,
                }
                for item in versions
            ],
            "latestVersion": versions[0].version_number if versions else None,
        }

    @router.patch("/api/tasks/{task_id}")
    async def edit_task(task_id: str, body: TaskBody) -> dict[str, Any]:
        if database.get_account(body.definition.account) is None:
            raise APIError("VALIDATION_FAILED", "Telegram 账号不存在", 422)
        task = service.edit_task(task_id, body.definition)
        return _task_json(task, database, service)

    @router.post("/api/tasks/{task_id}/publish")
    async def publish_task(task_id: str, body: PublishBody) -> dict[str, Any]:
        service.publish_task(task_id, body.release_note)
        task = database.get_task_any(task_id)
        if task is None:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        return _task_json(task, database, service)

    async def task_action(task_id: str, action: str) -> dict[str, Any]:
        task = getattr(service, f"{action}_task")(task_id)
        return _task_json(task, database, service)

    @router.post("/api/tasks/{task_id}/enable")
    async def enable_task(task_id: str, _: EmptyBody):
        return await task_action(task_id, "enable")

    @router.post("/api/tasks/{task_id}/disable")
    async def disable_task(task_id: str, _: EmptyBody):
        return await task_action(task_id, "disable")

    @router.post("/api/tasks/{task_id}/skip-next")
    async def skip_next_task(task_id: str, _: EmptyBody):
        return await task_action(task_id, "skip_next")

    @router.post("/api/tasks/{task_id}/archive")
    async def archive_task(task_id: str, _: EmptyBody):
        return await task_action(task_id, "archive")

    @router.post("/api/tasks/{task_id}/restore")
    async def restore_task(task_id: str, _: EmptyBody):
        return await task_action(task_id, "restore")

    @router.post("/api/tasks/{task_id}/run", status_code=202)
    async def run_task(task_id: str, _: EmptyBody) -> dict[str, Any]:
        run_id = service.start_manual_run(task_id)
        task = database.get_task_any(task_id)
        if task is None:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        payload = _task_json(task, database, service)
        payload["runId"] = run_id
        return payload

    @router.post("/api/tasks/{task_id}/test", status_code=202)
    async def test_task(task_id: str, body: TaskBody) -> dict[str, Any]:
        if database.get_account(body.definition.account) is None:
            raise APIError("VALIDATION_FAILED", "Telegram 账号不存在", 422)
        run_id = service.start_test_run(task_id, body.definition)
        task = database.get_task_any(task_id)
        if task is None:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        payload = _task_json(task, database, service)
        payload["runId"] = run_id
        return payload

    @router.post("/api/tasks/{task_id}/cancel", status_code=202)
    async def cancel_task(task_id: str, _: EmptyBody) -> dict[str, Any]:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        if not await service.cancel_task(task.id):
            raise APIError("RUN_NOT_ACTIVE", "任务当前没有运行实例", 409)
        return {"accepted": True, "taskId": task.id}

    return router
