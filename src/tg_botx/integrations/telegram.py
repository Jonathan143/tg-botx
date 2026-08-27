"""Telethon construction helpers kept at the integration boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from telethon import TelegramClient


@dataclass(frozen=True, slots=True)
class TelegramAccountConfig:
    """The minimum data needed to construct a Telethon session."""

    api_id: int
    api_hash: str
    session_path: Path


class TelegramClientProvider(Protocol):
    def __call__(self, config: TelegramAccountConfig) -> TelegramClient: ...


def create_telethon_client(config: TelegramAccountConfig) -> TelegramClient:
    """Build a Telethon client for an adapter or application service."""

    return TelegramClient(str(config.session_path), config.api_id, config.api_hash)
