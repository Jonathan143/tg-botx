"""Shared UTC clock and timestamp serialization helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_isoformat(value: datetime | None, *, timespec: str = "seconds") -> str | None:
    """Serialize a datetime as a canonical UTC RFC 3339 timestamp.

    Naive values are treated as UTC for compatibility with legacy persisted
    data. All serialized UTC values use ``Z`` and a fixed precision by default.
    """

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")
