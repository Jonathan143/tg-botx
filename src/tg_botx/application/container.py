"""CLI、HTTP 与调度进程共用的依赖组装和资源所有权。"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from tg_botx.config import Settings
from tg_botx.features.accounts.service import LoginFlowManager
from tg_botx.features.checkin.notifications import NotificationService
from tg_botx.features.checkin.runtime import CheckinService
from tg_botx.infrastructure.observability.log_stream import LogStream
from tg_botx.infrastructure.persistence.db import Database
from tg_botx.integrations.client_pool import ClientPool
from tg_botx.interfaces.admin.admin_security import (
    FailureRateLimiter,
    SessionManager,
    TransportKeyManager,
)
from tg_botx.interfaces.telegram.runtime import TelegramManagementBot


def build_database(settings: Settings, *, initialize_schema: bool = True) -> Database:
    settings.ensure_directories()
    database = Database(settings.database_url)
    if initialize_schema:
        database.create_all()
    return database


@dataclass(slots=True)
class ApplicationContext:
    settings: Settings
    database: Database
    checkin: CheckinService
    owns_database: bool = False

    async def close(self) -> None:
        try:
            await self.checkin.close()
        finally:
            if self.owns_database:
                self.database.engine.dispose()


def build_context(
    settings: Settings | None = None,
    database: Database | None = None,
    *,
    initialize_schema: bool = True,
) -> ApplicationContext:
    resolved_settings = settings if settings is not None else Settings()
    resolved_settings.ensure_directories()
    resolved_database = (
        database
        if database is not None
        else build_database(resolved_settings, initialize_schema=initialize_schema)
    )
    if database is not None and initialize_schema:
        database.create_all()
    return ApplicationContext(
        resolved_settings,
        resolved_database,
        CheckinService(
            resolved_settings,
            resolved_database,
            pool=ClientPool(resolved_settings),
            notifications=NotificationService(resolved_settings),
            scheduler=AsyncIOScheduler(timezone="UTC"),
        ),
        owns_database=database is None,
    )


@dataclass(slots=True)
class AdminContext:
    application: ApplicationContext
    admin_key: str | bytes
    admin_origin: str
    keys: TransportKeyManager
    sessions: SessionManager
    limiter: FailureRateLimiter
    accounts: LoginFlowManager
    bot: TelegramManagementBot
    trusted_proxies: list[str]
    logs: LogStream
    shutdown: asyncio.Event

    async def close(self) -> None:
        self.shutdown.set()
        # All resources must be closed even when an earlier closer fails.
        try:
            await self.logs.close()
        finally:
            try:
                await self.bot.close()
            finally:
                try:
                    await self.accounts.close()
                finally:
                    await self.application.close()


def build_admin_context(application: ApplicationContext) -> AdminContext:
    settings, database, checkin = application.settings, application.database, application.checkin
    admin_key, admin_origin = settings.require_admin_config()
    proxies = [item.strip() for item in settings.trusted_proxies.split(",") if item.strip()]
    try:
        for proxy in proxies:
            ipaddress.ip_network(proxy, strict=False)
    except ValueError as exc:
        raise RuntimeError("TG_BOT_TRUSTED_PROXIES 包含无效 CIDR") from exc
    return AdminContext(
        application,
        admin_key,
        admin_origin,
        TransportKeyManager(rotation_hours=settings.transport_key_rotation_hours),
        SessionManager(admin_key, session_days=settings.admin_session_days, session_store=database),
        FailureRateLimiter(max_failures=5, window_seconds=600),
        LoginFlowManager(settings, database, client_pool=checkin.pool),
        TelegramManagementBot(settings, database, checkin),
        proxies,
        LogStream(settings),
        asyncio.Event(),
    )
