"""Small in-process event bus used to decouple features.

The bus is deliberately process-local.  It is useful for wiring task status
updates to notifications or monitoring in one worker; deployments that need
cross-process delivery can replace it with a broker-backed adapter later.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """An immutable event emitted by an application service."""

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EventSubscription:
    """Async iterator returned by :meth:`EventBus.subscribe`."""

    def __init__(self, bus: "EventBus", event_name: str | None):
        self._bus = bus
        self._event_name = event_name
        self._queue: asyncio.Queue[DomainEvent] = asyncio.Queue()
        self._closed = False
        bus._subscriptions.add(self)

    def accepts(self, event: DomainEvent) -> bool:
        return self._event_name is None or self._event_name == event.name

    def push(self, event: DomainEvent) -> None:
        if not self._closed and self.accepts(event):
            self._queue.put_nowait(event)

    def __aiter__(self) -> AsyncIterator[DomainEvent]:
        return self

    async def __anext__(self) -> DomainEvent:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._subscriptions.discard(self)


class EventBus:
    """Publish events to in-process subscribers without shared global state."""

    def __init__(self) -> None:
        self._subscriptions: set[EventSubscription] = set()

    def subscribe(self, event_name: str | None = None) -> EventSubscription:
        return EventSubscription(self, event_name)

    async def publish(self, event: DomainEvent) -> None:
        for subscription in tuple(self._subscriptions):
            subscription.push(event)
