from __future__ import annotations

import inspect
import logging
from typing import Any

from tg_botx.features.accounts.models import (
    AdminAccountError,
)
from tg_botx.infrastructure.persistence.db import Account

logger = logging.getLogger(__name__)


class AccountAccess:
    def __init__(self, settings, database, client_factory, client_pool):
        self.settings = settings
        self.database = database
        self._client_factory = client_factory
        self._client_pool = client_pool

    async def _get_pooled_client(self, account: Account) -> Any:
        if self._client_pool is None:
            raise AdminAccountError("TELEGRAM_UNAVAILABLE", "Telegram 服务暂不可用")
        try:
            return await self._client_pool.get(account)
        except Exception:
            raise AdminAccountError("TELEGRAM_UNAVAILABLE", "无法连接 Telegram 账号") from None

    async def _acquire_pooled_client(self, account: Account) -> Any:
        if self._client_pool is None:
            raise AdminAccountError("TELEGRAM_UNAVAILABLE", "Telegram 服务暂不可用")
        try:
            acquire = getattr(self._client_pool, "acquire", None)
            return await (
                acquire(account) if acquire is not None else self._client_pool.get(account)
            )
        except Exception:
            raise AdminAccountError("TELEGRAM_UNAVAILABLE", "无法连接 Telegram 账号") from None

    async def _release_pooled_client(self, account: Account) -> None:
        if self._client_pool is None:
            return
        release = getattr(self._client_pool, "release", None)
        if release is None:
            return
        try:
            await release(account)
        except Exception:
            logger.warning("释放 Telegram 账号连接失败 account_id=%s", account.id, exc_info=True)

    async def _disconnect_pooled_client(self, account: Account) -> None:
        if self._client_pool is None:
            return
        remover = getattr(self._client_pool, "disconnect_account", None)
        if remover is not None:
            result = remover(account)
            if inspect.isawaitable(result):
                result = await result
            if result is False:
                raise AdminAccountError("ACCOUNT_BUSY", "账号当前正在执行任务，请稍后再试")
            return
        clients = getattr(self._client_pool, "clients", None)
        if isinstance(clients, dict):
            client = clients.pop(account.id, None)
            if client is not None:
                result = client.disconnect()
                if inspect.isawaitable(result):
                    await result

    def _ensure_pooled_client_idle(self, account: Account) -> None:
        if self._client_pool is None:
            return
        checker = getattr(self._client_pool, "has_active_leases", None)
        if checker is not None and checker(account):
            raise AdminAccountError("ACCOUNT_BUSY", "账号当前正在执行任务，请稍后再试")

    def _credentials(self) -> tuple[int, str]:
        try:
            return self.settings.require_api_credentials()
        except Exception:
            raise AdminAccountError(
                "TELEGRAM_CONFIGURATION_INVALID", "Telegram API 配置不可用"
            ) from None

    def _find_account(self, account_id_or_name: str) -> Account | None:
        return self.database.get_account_by_id(account_id_or_name) or self.database.get_account(
            account_id_or_name
        )
