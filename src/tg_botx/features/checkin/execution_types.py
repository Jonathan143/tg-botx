from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from tg_botx.features.checkin.condition import (
    ConditionVariable,
)


class CheckinError(RuntimeError):
    def __init__(
        self,
        message: str,
        bot_response: str | None = None,
        bot_buttons: list[list[str]] | None = None,
    ):
        super().__init__(message)
        self.bot_response = bot_response
        self.bot_buttons = bot_buttons


@dataclass(slots=True)
class ExecutionContext:
    entity: Any
    bot_id: int | None
    timezone: ZoneInfo
    baseline: int
    current_message: Any = None
    last_wait_message: Any = None
    last_wait_text: str | None = None
    last_wait_metadata: dict[str, Any] = field(default_factory=dict)
    last_clicked_callback_data_text: str | None = None
    last_clicked_callback_data_base64: str | None = None
    bot_response: str | None = None
    bot_buttons: list[list[str]] | None = None
    editable_message_ids: set[int] = field(default_factory=set)
    editable_message_texts: dict[int, str] = field(default_factory=dict)
    variables: dict[str, ConditionVariable] = field(default_factory=dict)
    http_response: Any = None
    http_responses: dict[str, Any] = field(default_factory=dict)
    wait_messages: dict[str, str] = field(default_factory=dict)
    wait_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class StepReport:
    response_reported: bool = False
    condition_reported: bool = False
