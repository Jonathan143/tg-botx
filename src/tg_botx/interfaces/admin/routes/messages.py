from __future__ import annotations

from fastapi import APIRouter

from tg_botx.features.message_library.models import (
    MessageGroupConflict,
    MessageGroupDelete,
    MessageGroupDetail,
    MessageGroupNotFound,
    MessageGroupSummary,
    MessageGroupUpdate,
    MessageGroupWrite,
)
from tg_botx.features.message_library.service import MessageLibraryService
from tg_botx.infrastructure.persistence.db import Database
from tg_botx.interfaces.admin.errors import APIError


def build_router(database: Database) -> APIRouter:
    router = APIRouter(prefix="/api/message-library/groups")
    library = MessageLibraryService(database)

    @router.get("")
    def list_groups() -> dict[str, list[MessageGroupSummary]]:
        return {"items": database.messages.list_groups()}

    @router.post("", status_code=201)
    def create_group(body: MessageGroupWrite) -> MessageGroupDetail:
        try:
            return database.messages.create_group(body)
        except MessageGroupConflict as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc

    @router.get("/{group_id}")
    def get_group(group_id: str) -> MessageGroupDetail:
        try:
            return database.messages.get_group(group_id)
        except MessageGroupNotFound as exc:
            raise APIError("NOT_FOUND", str(exc), 404) from exc

    @router.put("/{group_id}")
    def update_group(group_id: str, body: MessageGroupUpdate) -> MessageGroupDetail:
        try:
            return library.update_group(group_id, body, body.revision)
        except MessageGroupNotFound as exc:
            raise APIError("NOT_FOUND", str(exc), 404) from exc
        except MessageGroupConflict as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc

    @router.delete("/{group_id}")
    def delete_group(group_id: str, body: MessageGroupDelete) -> dict[str, bool]:
        try:
            library.delete_group(group_id, body.revision)
        except MessageGroupNotFound as exc:
            raise APIError("NOT_FOUND", str(exc), 404) from exc
        except MessageGroupConflict as exc:
            raise APIError("CONFLICT", str(exc), 409) from exc
        return {"deleted": True}

    return router
