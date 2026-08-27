"""Channel notification contracts and a transport-agnostic service."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ChannelNotification:
    """A message to publish to one Telegram channel."""

    channel: int | str
    text: str
    parse_mode: str | None = None
    disable_preview: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ChannelTransport(Protocol):
    def send(self, notification: ChannelNotification) -> None | Awaitable[None]: ...


class ChannelNotifier:
    """Publish notifications through an injected transport.

    The service does not know whether transport is Telethon, the Bot API, or
    a queue, making channel maintenance independently testable.
    """

    def __init__(
        self, transport: ChannelTransport | Callable[[ChannelNotification], object]
    ):
        self._transport = transport

    async def publish(self, notification: ChannelNotification) -> None:
        if hasattr(self._transport, "send"):
            result = self._transport.send(notification)
        else:
            result = self._transport(notification)  # type: ignore[operator]
        if inspect.isawaitable(result):
            await result
