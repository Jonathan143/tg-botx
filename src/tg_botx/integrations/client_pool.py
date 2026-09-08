from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from telethon import TelegramClient

from tg_botx.config import Settings
from tg_botx.infrastructure.persistence.db import (
    Account,
)
from tg_botx.integrations.telegram import TelegramAccountConfig, create_telethon_client

logger = logging.getLogger(__name__)


class ClientPool:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.clients: dict[str, TelegramClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # A client is shared by concurrent runs for one account.  Keep it
        # connected only while at least one caller holds a lease.
        self._leases: dict[str, int] = {}
        self._pending_acquires: dict[str, int] = {}

    def _lock_for(self, account_id: str) -> asyncio.Lock:
        lock = self._locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account_id] = lock
        return lock

    async def get(self, account: Account) -> TelegramClient:
        # Telethon SQLite sessions cannot be shared. Serialize construction so
        # parallel tasks for the same account reuse one connected client.
        async with self._lock_for(account.id):
            return await self._get_unlocked(account)

    async def _get_unlocked(self, account: Account) -> TelegramClient:
        client = self.clients.get(account.id)
        if client is not None:
            return client
        api_id, api_hash = self.settings.require_api_credentials()
        client = create_telethon_client(
            TelegramAccountConfig(
                api_id=api_id,
                api_hash=api_hash,
                session_path=self.settings.sessions_dir / account.session_name,
            )
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError(f"账号 {account.name} 尚未登录，请先执行 tg-bot login")
        except Exception:
            with suppress(Exception):
                await client.disconnect()
            raise
        self.clients[account.id] = client
        return client

    async def acquire(self, account: Account) -> TelegramClient:
        """Get a connected client and hold an account-level lease."""

        # Mark the acquisition before awaiting ``get`` so a concurrent final
        # release cannot disconnect the client in the middle of this handoff.
        async with self._lock_for(account.id):
            self._pending_acquires[account.id] = self._pending_acquires.get(account.id, 0) + 1
        try:
            # Keep the public ``get`` seam usable for adapters/tests that
            # provide their own client factory.
            client = await self.get(account)
        finally:
            async with self._lock_for(account.id):
                pending = self._pending_acquires.get(account.id, 0)
                if pending <= 1:
                    self._pending_acquires.pop(account.id, None)
                else:
                    self._pending_acquires[account.id] = pending - 1
        async with self._lock_for(account.id):
            pooled = self.clients.get(account.id)
            if pooled is None:
                pooled = client
                self.clients[account.id] = pooled
            client = pooled
            self._leases[account.id] = self._leases.get(account.id, 0) + 1
            return client

    async def release(self, account: Account) -> None:
        """Release one lease and disconnect when the account becomes idle."""

        async with self._lock_for(account.id):
            leases = self._leases.get(account.id, 0)
            if leases <= 0:
                return
            if leases > 0:
                leases -= 1
                if leases:
                    self._leases[account.id] = leases
                else:
                    self._leases.pop(account.id, None)
            if leases == 0 and self._pending_acquires.get(account.id, 0) == 0:
                client = self.clients.pop(account.id, None)
                if client is not None:
                    with suppress(Exception):
                        await client.disconnect()

    def has_active_leases(self, account: Account) -> bool:
        return self._leases.get(account.id, 0) > 0

    async def disconnect_account(self, account: Account) -> bool:
        async with self._lock_for(account.id):
            if self._leases.get(account.id, 0) > 0 or self._pending_acquires.get(account.id, 0) > 0:
                return False
            self._leases.pop(account.id, None)
            client = self.clients.pop(account.id, None)
            if client is None:
                return True
            with suppress(Exception):
                await client.disconnect()
            return True

    async def close(self) -> None:
        for account_id in list(self.clients):
            async with self._lock_for(account_id):
                client = self.clients.pop(account_id, None)
                self._leases.pop(account_id, None)
                self._pending_acquires.pop(account_id, None)
                if client is None:
                    continue
                with suppress(Exception):
                    await client.disconnect()
