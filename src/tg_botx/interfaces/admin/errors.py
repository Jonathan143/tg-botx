from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        details: Any | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        self.headers = headers or {}


def _validation_details(exc: Exception) -> list[dict[str, Any]]:
    if hasattr(exc, "errors"):
        items = []
        for error in exc.errors():
            items.append(
                {
                    "path": ".".join(str(part) for part in error.get("loc", ())),
                    "message": error.get("msg", "输入无效"),
                    "type": error.get("type", "validation_error"),
                }
            )
        return items
    return [{"path": "", "message": "输入无效", "type": "validation_error"}]
