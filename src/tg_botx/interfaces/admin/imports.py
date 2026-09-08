from __future__ import annotations

import logging

import yaml

from tg_botx.interfaces.admin.errors import APIError, _validation_details
from tg_botx.schemas import TaskDefinition

logger = logging.getLogger(__name__)


def _parse_task_yaml(value: str) -> TaskDefinition:
    try:
        raw = yaml.safe_load(value)
        if not isinstance(raw, dict):
            raise ValueError("YAML 顶层必须是对象")
        return TaskDefinition.model_validate(raw)
    except Exception as exc:
        raise APIError(
            "VALIDATION_FAILED", "YAML 任务配置无效", 422, details=_validation_details(exc)
        ) from exc
