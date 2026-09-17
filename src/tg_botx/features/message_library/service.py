from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from tg_botx.features.checkin.errors import TaskStateError
from tg_botx.features.message_library.models import (
    MessageGroupConflict,
    MessageGroupDetail,
    MessageGroupNotFound,
    MessageGroupWrite,
)

if TYPE_CHECKING:
    from tg_botx.infrastructure.persistence.db import Database
    from tg_botx.schemas import TaskDefinition


def _steps(items: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for step in items:
        yield step
        if step.get("type") == "condition":
            for branch in step.get("branches", []):
                yield from _steps(branch.get("steps", []))


def _uses_group(items: list[dict[str, Any]], group_id: str) -> bool:
    return any(
        step.get("type") == "send_message"
        and step.get("message_mode") == "random"
        and step.get("random_source") == "group"
        and step.get("message_group_id") == group_id
        for step in _steps(items)
    )


class MessageLibraryService:
    def __init__(self, database: Database):
        self.database = database

    def _ensure_unreferenced(self, group_id: str) -> None:
        for task in self.database.list_tasks(include_archived=True):
            version = self.database.get_latest_workflow_version(task.id)
            if _uses_group(task.config.get("steps", []), group_id) or (
                version is not None
                and _uses_group(version.execution_definition.get("steps", []), group_id)
            ):
                raise MessageGroupConflict(
                    "分组仍被任务草稿或已发布工作流引用，请先更换消息来源并重新发布"
                )

    def update_group(
        self, group_id: str, payload: MessageGroupWrite, revision: int
    ) -> MessageGroupDetail:
        self.database.messages.get_group(group_id)
        if not payload.messages:
            self._ensure_unreferenced(group_id)
        return self.database.messages.update_group(group_id, payload, revision)

    def delete_group(self, group_id: str, revision: int) -> None:
        self.database.messages.get_group(group_id)
        self._ensure_unreferenced(group_id)
        self.database.messages.delete_group(group_id, revision)


def validate_task_message_groups(database: Database, definition: TaskDefinition) -> None:
    """Validate live references without replacing them with a persisted snapshot."""
    from tg_botx.schemas import TaskDefinition

    payload = definition.model_dump(mode="json")
    resolved: dict[str, list[str]] = {}
    try:
        for step in _steps(payload["steps"]):
            if (
                step.get("type") == "send_message"
                and step.get("message_mode") == "random"
                and step.get("random_source") == "group"
            ):
                group_id = step["message_group_id"]
                if group_id not in resolved:
                    resolved[group_id] = database.messages.get_messages(group_id)
                step.pop("message_group_id")
                step["random_source"] = "manual"
                step["messages"] = resolved[group_id]
        if resolved:
            # Reuse workflow scope validation for every candidate, including
            # candidates inside nested branches. The original definition stays live.
            TaskDefinition.model_validate(payload)
    except (MessageGroupNotFound, MessageGroupConflict) as exc:
        raise TaskStateError(str(exc)) from exc
    except ValidationError as exc:
        raise TaskStateError("消息分组内容与当前工作流不兼容，请检查消息模板引用的变量") from exc
