from __future__ import annotations

import json
from collections.abc import Callable

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tg_botx.features.message_library.models import (
    MessageGroupConflict,
    MessageGroupDetail,
    MessageGroupNotFound,
    MessageGroupSummary,
    MessageGroupWrite,
)
from tg_botx.infrastructure.persistence.message_models import MessageGroup
from tg_botx.infrastructure.persistence.models import utc_now


class MessagesRepository:
    def __init__(self, session_factory: Callable[[], Session]):
        self._session = session_factory

    def list_groups(self) -> list[MessageGroupSummary]:
        # Do not load message bodies for navigation and group selectors.
        statement = select(
            MessageGroup.id,
            MessageGroup.name,
            MessageGroup.message_count,
            MessageGroup.revision,
            MessageGroup.created_at,
            MessageGroup.updated_at,
        ).order_by(MessageGroup.name, MessageGroup.id)
        with self._session() as session:
            return [
                MessageGroupSummary.model_validate(row)
                for row in session.execute(statement).mappings()
            ]

    def get_group(self, group_id: str) -> MessageGroupDetail:
        with self._session() as session:
            group = session.get(MessageGroup, group_id)
            if group is None:
                raise MessageGroupNotFound("消息分组不存在，请重新选择分组")
            return MessageGroupDetail.model_validate(group)

    def get_messages(self, group_id: str) -> list[str]:
        messages = self.get_group(group_id).messages
        if not messages:
            raise MessageGroupConflict("消息分组为空，请先在消息库中添加消息")
        return messages.copy()

    def create_group(self, payload: MessageGroupWrite) -> MessageGroupDetail:
        group = MessageGroup(
            name=payload.name,
            messages_json=json.dumps(payload.messages, ensure_ascii=False),
            message_count=len(payload.messages),
            revision=1,
        )
        try:
            with self._session() as session:
                session.add(group)
                session.commit()
                return MessageGroupDetail.model_validate(group)
        except IntegrityError as exc:
            raise MessageGroupConflict("分组名称已存在") from exc

    @staticmethod
    def _raise_write_conflict(session: Session, group_id: str) -> None:
        if session.get(MessageGroup, group_id) is None:
            raise MessageGroupNotFound("消息分组不存在，请刷新消息库")
        raise MessageGroupConflict("分组已被其他操作修改，请刷新后重试")

    def update_group(
        self, group_id: str, payload: MessageGroupWrite, revision: int
    ) -> MessageGroupDetail:
        try:
            with self._session() as session:
                statement = (
                    update(MessageGroup)
                    .where(MessageGroup.id == group_id, MessageGroup.revision == revision)
                    .values(
                        name=payload.name,
                        messages_json=json.dumps(payload.messages, ensure_ascii=False),
                        message_count=len(payload.messages),
                        revision=revision + 1,
                        updated_at=utc_now(),
                    )
                    .returning(MessageGroup.id)
                )
                if session.scalar(statement) is None:
                    self._raise_write_conflict(session, group_id)
                session.commit()
                group = session.get(MessageGroup, group_id)
                if group is None:
                    raise MessageGroupNotFound("消息分组不存在，请刷新消息库")
                return MessageGroupDetail.model_validate(group)
        except IntegrityError as exc:
            raise MessageGroupConflict("分组名称已存在") from exc

    def delete_group(self, group_id: str, revision: int) -> None:
        with self._session() as session:
            statement = (
                delete(MessageGroup)
                .where(MessageGroup.id == group_id, MessageGroup.revision == revision)
                .returning(MessageGroup.id)
            )
            if session.scalar(statement) is None:
                self._raise_write_conflict(session, group_id)
            session.commit()
