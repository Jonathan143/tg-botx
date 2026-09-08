from __future__ import annotations

import logging

from tg_botx.features.bot.models import (
    _CHECKIN_MAX_KEY,
    _CHECKIN_MIN_KEY,
    _DEFAULT_CHECKIN_MAX,
    _DEFAULT_CHECKIN_MIN,
    BotBindingError,
)
from tg_botx.infrastructure.persistence.db import (
    Database,
)

logger = logging.getLogger(__name__)


class BotPointsService:
    def __init__(self, database: Database):
        self.database = database

    def checkin_config(self) -> dict[str, int]:
        getter = getattr(self.database, "get_bot_setting", None)

        def read(key: str, fallback: int) -> int:
            if not callable(getter):
                return fallback
            try:
                value = int(getter(key) or fallback)
            except (TypeError, ValueError):
                return fallback
            return value if value >= 1 else fallback

        minimum = read(_CHECKIN_MIN_KEY, _DEFAULT_CHECKIN_MIN)
        maximum = read(_CHECKIN_MAX_KEY, _DEFAULT_CHECKIN_MAX)
        if maximum < minimum:
            maximum = minimum
        return {"minPoints": minimum, "maxPoints": maximum}

    def update_checkin_config(self, minimum: int, maximum: int) -> dict[str, int]:
        if minimum < 1 or maximum < 1 or minimum > maximum:
            raise BotBindingError("积分随机范围无效，需满足 1 ≤ 最小值 ≤ 最大值")
        if maximum > 1_000_000:
            raise BotBindingError("积分随机范围不能超过 1000000")
        self.database.set_bot_setting(_CHECKIN_MIN_KEY, str(minimum))
        self.database.set_bot_setting(_CHECKIN_MAX_KEY, str(maximum))
        return {"minPoints": minimum, "maxPoints": maximum}

    def checkin(self, user_id: int, chat_id: int) -> tuple[str, int, int]:
        config = self.checkin_config()
        return self.database.checkin_bot_user(
            user_id, chat_id, config["minPoints"], config["maxPoints"]
        )
