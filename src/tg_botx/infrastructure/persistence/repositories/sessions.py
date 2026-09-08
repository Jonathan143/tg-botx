from __future__ import annotations

from datetime import datetime

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
