"""Telethon construction helpers kept at the integration boundary."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from telethon import TelegramClient
from telethon.sessions import SQLiteSession


@dataclass(frozen=True, slots=True)
class TelegramAccountConfig:
    """The minimum data needed to construct a Telethon session."""

    api_id: int
    api_hash: str
    session_path: Path


class TelegramClientProvider(Protocol):
    def __call__(self, config: TelegramAccountConfig) -> TelegramClient: ...


class TelegramSQLiteSession(SQLiteSession):
    """SQLite session that waits for writers instead of failing immediately.

    Telethon's default connection uses a 5s timeout and DELETE journals.  A
    second client on the same file then surfaces ``database is locked`` during
    keepalive or reconnect.  WAL plus a longer busy timeout keeps brief
    contention from tearing down the connection.
    """

    _BUSY_TIMEOUT_SECONDS = 30.0

    def _cursor(self):
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.filename,
                timeout=self._BUSY_TIMEOUT_SECONDS,
                check_same_thread=False,
            )
            self._conn.execute(
                f"PRAGMA busy_timeout={int(self._BUSY_TIMEOUT_SECONDS * 1000)}"
            )
            self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
        return self._conn.cursor()


def create_telethon_client(config: TelegramAccountConfig) -> TelegramClient:
    """Build a Telethon client for an adapter or application service."""

    return TelegramClient(
        TelegramSQLiteSession(str(config.session_path)),
        config.api_id,
        config.api_hash,
    )
