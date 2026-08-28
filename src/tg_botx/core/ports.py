"""Dependency-inversion ports shared by application services."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, TypeVar

T = TypeVar("T", contravariant=True)


class AsyncSender(Protocol[T]):
    """An async boundary for sending a value to an external system."""

    async def send(self, value: T) -> None: ...


class AsyncSummarizer(Protocol[T]):
    """An async boundary for summarizing a sequence of values."""

    async def summarize(self, values: Sequence[T]) -> str: ...


class Clock(Protocol):
    """Minimal clock abstraction for deterministic scheduling tests."""

    def now(self) -> datetime: ...
