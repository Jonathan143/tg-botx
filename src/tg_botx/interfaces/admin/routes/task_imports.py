from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import Response

from tg_botx.features.checkin.runtime import (
    CheckinService,
    TaskStateError,
)
from tg_botx.infrastructure.persistence.db import (
    Database,
)
from tg_botx.interfaces.admin.errors import APIError
from tg_botx.interfaces.admin.imports import _parse_task_yaml
from tg_botx.interfaces.admin.models import ImportBody
from tg_botx.interfaces.admin.presenters import _task_json
from tg_botx.schemas import TaskDefinition

logger = logging.getLogger(__name__)


def build_router(database: Database, service: CheckinService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/tasks/{task_id}/export")
    async def export_task(task_id: str) -> Response:
        task = database.get_task_any(task_id)
        if not task:
            raise APIError("NOT_FOUND", "任务不存在", 404)
        content = TaskDefinition.model_validate(task.config).to_yaml()
        return Response(
            content,
            media_type="application/yaml; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="task-{task.id}.yaml"'},
        )

    @router.post("/api/tasks/import/preflight")
    async def import_preflight(body: ImportBody) -> dict[str, Any]:
        try:
            definition = _parse_task_yaml(body.yaml)
        except APIError as exc:
            return {
                "valid": False,
                "conflicts": [],
                "task": None,
                "errors": exc.details or [],
            }
        existing = database.get_task_any(definition.name)
        errors = []
        if database.get_account(definition.account) is None:
            errors.append({"path": "account", "message": "Telegram 账号不存在"})
        return {
            "valid": not errors,
            "conflicts": [definition.name] if existing else [],
            "task": definition.to_api_dict(),
            "errors": errors,
        }

    @router.post("/api/tasks/import")
    async def import_task(body: ImportBody) -> dict[str, Any]:
        definition = _parse_task_yaml(body.yaml)
        if database.get_account(definition.account) is None:
            raise APIError("VALIDATION_FAILED", "Telegram 账号不存在", 422)
        existing = database.get_task_any(definition.name)
        if existing and definition.name not in body.overwrite_names:
            raise APIError(
                "CONFLICT",
                "同名任务已存在，必须明确选择覆盖",
                409,
                details={"conflicts": [definition.name]},
            )
        try:
            if existing and existing.archived:
                service.restore_task(existing.id)
            task = (
                service.edit_task(existing.id, definition)
                if existing
                else service.create_task(definition)
            )
        except TaskStateError as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc
        return {"imported": [_task_json(task, database, service)], "overwritten": bool(existing)}

    return router
