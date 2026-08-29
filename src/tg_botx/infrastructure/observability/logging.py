from __future__ import annotations

import copy
import logging
import re
from collections.abc import Iterable
from pathlib import Path


class IconFormatter(logging.Formatter):
    """Keep the machine-readable log prefix while adding a visual level cue."""

    _ICONS = {
        logging.DEBUG: "🔎",
        logging.INFO: "ℹ️",
        logging.WARNING: "⚠️",
        logging.ERROR: "❌",
        logging.CRITICAL: "💥",
    }

    def format(self, record: logging.LogRecord) -> str:
        # Format a shallow copy so handlers do not mutate the shared record
        # (and so the icon is added exactly once when multiple handlers run).
        rendered = copy.copy(record)
        message = rendered.getMessage()
        icon = self._ICONS.get(rendered.levelno)
        if icon and not message.startswith(tuple(self._ICONS.values())):
            rendered.msg = f"{icon} {message}"
            rendered.args = ()
        return super().format(rendered)


_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)(api[_-]?hash|admin[_-]?key|password|2fa|code|验证码|管理密钥|"
            r"notification[_-]?bot[_-]?token|phone|手机号)\s*([=:]\s*|\"\s*:\s*\")"
            r"([^\s,;&\"}]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"https://api\.telegram\.org/bot[^/\s]+", re.IGNORECASE),
     "https://api.telegram.org/bot[REDACTED]"),
    (re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:\s/]+:)([^@\s]+)(@)"), r"\1[REDACTED]\3"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"), "[REDACTED_TOKEN]"),
    # Do not treat the date portion of an ISO-style log timestamp as a phone
    # number.  The generic phone pattern accepts hyphens, so values such as
    # ``2026-08-28`` were previously replaced before the admin log parser saw
    # them, turning an otherwise valid timestamp into ``[REDACTED_PHONE]``.
    (
        re.compile(
            r"(?<!\d)(?!\d{4}[-/]\d{1,2}[-/]\d{1,2}(?!\d))"
            r"\+?\d[\d\s()-]{7,}\d(?!\d)"
        ),
        "[REDACTED_PHONE]",
    ),
)


def redact_sensitive(value: str, secret_values: Iterable[str] = ()) -> str:
    redacted = value
    for secret in secret_values:
        if len(secret) >= 4:
            redacted = redacted.replace(secret, "[REDACTED]")
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class SensitiveDataFilter(logging.Filter):
    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        super().__init__()
        self.secret_values = tuple(value for value in secret_values if value)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = redact_sensitive(rendered, self.secret_values)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact_sensitive(record.exc_text, self.secret_values)
        return True


def allowed_log_files(log_path: Path, backup_count: int) -> list[Path]:
    """Return only the configured rotating log and its numbered backups."""
    candidates = [log_path]
    candidates.extend(Path(f"{log_path}.{index}") for index in range(1, backup_count + 1))
    return [path for path in candidates if path.is_file()]
