"""Shared UTC clock and timestamp serialization helpers."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo


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


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_local_time(value: datetime | None, timezone_name: str, *, seconds: bool = True) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if value is None:
        return "未安排"
    zone: tzinfo
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    pattern = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    return value.astimezone(zone).strftime(pattern) + f" ({timezone_name})"
