"""Dependency composition for command-line, API and worker entry points."""

from __future__ import annotations

from dataclasses import dataclass

from tg_botx.config import Settings
from tg_botx.features.checkin.runtime import CheckinService
from tg_botx.infrastructure.persistence.db import Database


@dataclass(slots=True)
class ApplicationContext:
    """Long-lived dependencies owned by one process."""

    settings: Settings
    database: Database
    checkin: CheckinService

    async def close(self) -> None:
        await self.checkin.close()


def build_context(
    settings: Settings | None = None, database: Database | None = None
) -> ApplicationContext:
    """Create the default application graph.

    ``settings`` and ``database`` are injectable for tests and for deployments
    that provide their own SQLAlchemy engine.
    """

    resolved_settings = settings or Settings()
    resolved_settings.ensure_directories()
    resolved_database = database or Database(resolved_settings.database_url)
    resolved_database.create_all()
    return ApplicationContext(
        settings=resolved_settings,
        database=resolved_database,
        checkin=CheckinService(resolved_settings, resolved_database),
    )
