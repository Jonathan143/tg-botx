"""Rule-based group monitoring primitives.

This module handles deterministic matching and reply decisions only.  A
Telegram adapter is responsible for receiving events and sending replies;
summarization can be supplied later without coupling the monitor to an LLM.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from tg_botx.features.checkin.matching import matches


@dataclass(frozen=True, slots=True)
class GroupMessage:
    chat_id: int | str
    message_id: int
    text: str
    sender_id: int | str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class MonitorRule:
    """A safe, declarative rule for one group message."""

    name: str
    pattern: Any
    reply: str | None = None
    enabled: bool = True

    def matches(self, message: GroupMessage) -> bool:
        return self.enabled and matches(message.text, self.pattern)


class MessageSummarizer(Protocol):
    def __call__(self, messages: tuple[GroupMessage, ...]) -> str | Awaitable[str]: ...


class GroupMonitor:
    """Evaluate rules and retain a bounded in-memory message window."""

    def __init__(
        self,
        rules: Iterable[MonitorRule] = (),
        *,
        max_history: int = 500,
        summarizer: MessageSummarizer | None = None,
    ) -> None:
        if max_history < 1:
            raise ValueError("max_history 必须大于 0")
        self.rules = list(rules)
        self.max_history = max_history
        self.summarizer = summarizer
        self._history: list[GroupMessage] = []

    @property
    def history(self) -> tuple[GroupMessage, ...]:
        return tuple(self._history)

    def add_rule(self, rule: MonitorRule) -> None:
        if any(item.name == rule.name for item in self.rules):
            raise ValueError(f"监控规则已存在：{rule.name}")
        self.rules.append(rule)

    def ingest(self, message: GroupMessage) -> tuple[str, ...]:
        self._history.append(message)
        del self._history[:-self.max_history]
        return tuple(rule.name for rule in self.rules if rule.matches(message))

    def reply_for(self, message: GroupMessage) -> str | None:
        for rule in self.rules:
            if rule.matches(message) and rule.reply:
                return rule.reply
        return None

    async def summarize(self, messages: Iterable[GroupMessage] | None = None) -> str | None:
        if self.summarizer is None:
            return None
        selected = tuple(messages) if messages is not None else self.history
        result = self.summarizer(selected)
        return await result if inspect.isawaitable(result) else result
