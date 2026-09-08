from __future__ import annotations

from datetime import datetime

from tg_botx.infrastructure.persistence.models import (
    AdminSession,
)


class SessionsRepository:
    def __init__(self, session_factory):
        self.session = session_factory

    def save_admin_session(
        self, token_hash: str, expires_at: datetime, last_seen_at: datetime
    ) -> None:
        with self.session() as session:
            item = session.get(AdminSession, token_hash)
            if item is None:
                item = AdminSession(token_hash=token_hash)
                session.add(item)
            item.expires_at = expires_at
            item.last_seen_at = last_seen_at
            session.commit()

    def get_admin_session(self, token_hash: str) -> AdminSession | None:
        with self.session() as session:
            return session.get(AdminSession, token_hash)

    def delete_admin_session(self, token_hash: str) -> None:
        with self.session() as session:
            item = session.get(AdminSession, token_hash)
            if item is not None:
                session.delete(item)
                session.commit()

    def delete_expired_admin_sessions(self, now: datetime) -> None:
        with self.session() as session:
            session.query(AdminSession).filter(AdminSession.expires_at <= now).delete(
                synchronize_session=False
            )
            session.commit()

    def delete_all_admin_sessions(self) -> None:
        with self.session() as session:
            session.query(AdminSession).delete(synchronize_session=False)
            session.commit()
