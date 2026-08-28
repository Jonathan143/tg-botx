from __future__ import annotations

from tg_botx.config import Settings
from tg_botx.infrastructure.observability.logging import redact_sensitive
from tg_botx.interfaces.admin.admin_api import _read_log_entries


def test_redaction_preserves_iso_log_timestamps() -> None:
    line = "2026-08-28 12:34:56,789 INFO tg_botx test message"

    assert redact_sensitive(line) == line


def test_log_reader_can_parse_timestamp_after_redaction(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path, log_file="test.log")
    settings.ensure_directories()
    settings.log_path.write_text(
        "2026-08-28 12:34:56,789 INFO tg_botx test message\n",
        encoding="utf-8",
    )

    entries = _read_log_entries(settings)

    assert entries == [
        {
            "timestamp": "2026-08-28 12:34:56,789",
            "level": "INFO",
            "logger": "tg_botx",
            "message": "test message",
            "source": "test.log",
        }
    ]
