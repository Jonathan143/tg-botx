from __future__ import annotations

import inspect
import logging

from tg_botx.features.accounts.models import (
    AccountTaskImpact,
    AccountView,
    AdminAccountError,
    LogoutImpact,
)
from tg_botx.infrastructure.persistence.db import Account

logger = logging.getLogger(__name__)


class AccountDirectory:
    def __init__(self, settings, database, access, client_factory):
        self.settings = settings
        self.database = database
        self.access = access
        self._client_factory = client_factory

    def list_accounts(self) -> list[AccountView]:
        accounts = self.database.list_accounts()
        tasks = self.database.list_tasks(include_archived=True)
        return [
            self._account_view(
                account,
                task_count=sum(task.account_id == account.id for task in tasks),
                enabled_task_count=sum(
                    task.account_id == account.id and task.enabled for task in tasks
                ),
            )
            for account in accounts
        ]

    def logout_impact(self, account_id_or_name: str) -> LogoutImpact:
        account = self.access._find_account(account_id_or_name)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        tasks = [
            AccountTaskImpact(
                task_id=task.id,
                name=task.name,
                enabled=task.enabled,
                archived=task.archived,
            )
            for task in self.database.list_tasks(include_archived=True)
            if task.account_id == account.id
        ]
        return LogoutImpact(account.id, account.name, tuple(tasks))

    async def logout(self, account_id_or_name: str) -> LogoutImpact:
        impact = self.logout_impact(account_id_or_name)
        if impact.enabled_task_ids:
            raise AdminAccountError("ACCOUNT_HAS_ENABLED_TASKS", "账号仍有关联的启用任务，无法退出")
        account = self.database.get_account_by_id(impact.account_id)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        self.access._ensure_pooled_client_idle(account)

        try:
            await self.access._disconnect_pooled_client(account)
            api_id, api_hash = self.access._credentials()
            client = self._client_factory(
                str(self.settings.sessions_dir / account.session_name), api_id, api_hash
            )
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
        except AdminAccountError:
            raise
        except Exception:
            raise AdminAccountError("ACCOUNT_LOGOUT_FAILED", "Telegram 账号退出失败") from None
        finally:
            if "client" in locals():
                try:
                    result = client.disconnect()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass

        self.database.deactivate_account(account.id)
        return impact

    @staticmethod
    def _account_view(
        account: Account, *, task_count: int = 0, enabled_task_count: int = 0
    ) -> AccountView:
        return AccountView(
            account_id=account.id,
            name=account.name,
            phone_masked=AccountDirectory._mask_phone(account.phone),
            session_name=account.session_name,
            is_active=account.is_active,
            created_at=account.created_at,
            task_count=task_count,
            enabled_task_count=enabled_task_count,
        )

    @staticmethod
    def _mask_phone(phone: str | None) -> str | None:
        if not phone:
            return None
        if len(phone) <= 4:
            return "*" * len(phone)
        return f"{phone[:3]}{'*' * (len(phone) - 5)}{phone[-2:]}"
