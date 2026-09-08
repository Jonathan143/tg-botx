from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from tg_botx.config import Settings
from tg_botx.infrastructure.observability.log_stream import LOG_PATTERN, log_secrets
from tg_botx.infrastructure.observability.logging import allowed_log_files, redact_sensitive

logger = logging.getLogger(__name__)


def _read_log_entries(settings: Settings) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    secrets = log_secrets(settings)
    # Oldest backup first, current log last.
    paths = list(reversed(allowed_log_files(settings.log_path, settings.log_backup_count)))
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            safe_line = redact_sensitive(line, secrets)
            matched = LOG_PATTERN.match(safe_line)
            if matched:
                item = matched.groupdict()
                item["source"] = path.name
                entries.append(item)
            elif entries:
                entries[-1]["message"] += "\n" + safe_line
            else:
                entries.append(
                    {
                        "timestamp": None,
                        "level": None,
                        "logger": None,
                        "message": safe_line,
                        "source": path.name,
                    }
                )
    return entries


def _filter_logs(
    entries: list[dict[str, Any]],
    level: str | None,
    query: str | None,
    started_from: datetime | None,
    started_to: datetime | None,
) -> list[dict[str, Any]]:
    if started_from:
        started_from = (
            started_from.replace(tzinfo=UTC)
            if started_from.tzinfo is None
            else started_from.astimezone(UTC)
        )
    if started_to:
        started_to = (
            started_to.replace(tzinfo=UTC)
            if started_to.tzinfo is None
            else started_to.astimezone(UTC)
        )
    wanted = level.upper() if level else None
    needle = query.casefold() if query else None
    result = []
    for item in entries:
        if wanted and item.get("level") != wanted:
            continue
        if needle and needle not in json.dumps(item, ensure_ascii=False).casefold():
            continue
        timestamp = item.get("timestamp")
        if timestamp and (started_from or started_to):
            try:
                parsed = datetime.fromisoformat(timestamp.replace(" ", "T", 1))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                if started_from and parsed < started_from:
                    continue
                if started_to and parsed > started_to:
                    continue
            except ValueError:
                pass
        result.append(item)
    return result
