from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tg_botx.infrastructure.persistence.models import Base, UTCDateTime, utc_now


class MessageGroup(Base):
    """A versioned group aggregate; task imports store their own independent copy."""

    __tablename__ = "message_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    messages_json: Mapped[str] = mapped_column(Text, default="[]")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

    @property
    def messages(self) -> Any:
        # The read DTO validates stored JSON too; corrupt data must never be sent.
        return json.loads(self.messages_json)
