"""Stable application contracts shared by Telegram features.

The core package intentionally contains no Telethon or database imports.  It
is safe to use from adapters, background workers and future web frontends.
"""

from tg_botx.core.events import DomainEvent, EventBus, EventSubscription
from tg_botx.core.ports import AsyncSender, AsyncSummarizer, Clock

__all__ = [
    "AsyncSender",
    "AsyncSummarizer",
    "Clock",
    "DomainEvent",
    "EventBus",
    "EventSubscription",
]
