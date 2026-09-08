from __future__ import annotations

import logging
import time
from typing import Any, Literal

from fastapi import APIRouter

from tg_botx.features.checkin.runtime import (
    CheckinService,
)
from tg_botx.infrastructure.persistence.db import (
    Database,
    utc_now,
)
from tg_botx.interfaces.admin.presenters import (
    _dashboard_trend,
    _iso,
    _run_json,
    _upcoming_task_json,
)

logger = logging.getLogger(__name__)


def build_router(database: Database, service: CheckinService, started_at: float) -> APIRouter:
    router = APIRouter()

    @router.get("/api/dashboard")
    async def dashboard(range: Literal["24h", "7d", "30d"] = "24h") -> dict[str, Any]:
        now = utc_now()
        since, status_breakdown = _dashboard_trend(database, range, now)
        raw_stats = database.dashboard_stats(since)
        success_runs = int(raw_stats["runs_success"])
        failed_runs = int(raw_stats["runs_failed"])
        completed_runs = success_runs + failed_runs
        total_tasks = int(raw_stats["tasks_total"])
        enabled_tasks = int(raw_stats["tasks_enabled"])
        running_tasks = len(service.running)
        success_rate = round(success_runs * 100 / completed_runs, 1) if completed_runs else 0.0
        stats = {
            "totalTasks": total_tasks,
            "enabledTasks": enabled_tasks,
            "runningTasks": running_tasks,
            "failedRuns": failed_runs,
            "successRate": success_rate,
            # Compatibility aliases retained for existing API consumers.
            "tasksTotal": raw_stats["tasks_total"],
            "tasksEnabled": raw_stats["tasks_enabled"],
            "tasksArchived": raw_stats["tasks_archived"],
            "runsTotal": raw_stats["runs_total"],
            "runsSuccess": raw_stats["runs_success"],
            "runsFailed": raw_stats["runs_failed"],
            "runsCanceled": raw_stats["runs_canceled"],
            "runsSkipped": raw_stats["runs_skipped"],
            "runsRunning": raw_stats["runs_running"],
        }
        account_items = database.list_accounts()
        active_accounts = sum(account.is_active for account in account_items)
        inactive_accounts = len(account_items) - active_accounts
        scheduler_health = "healthy" if service.scheduler.running else "unhealthy"
        telegram_health = (
            "healthy"
            if account_items and inactive_accounts == 0
            else "degraded"
            if active_accounts
            else "unhealthy"
        )
        database_health = "healthy"
        service_health = (
            "healthy"
            if scheduler_health == "healthy" and database_health == "healthy"
            else "degraded"
        )
        recent, _ = database.list_runs(page=1, page_size=10, started_from=since)
        upcoming = database.upcoming_tasks(limit=10)
        return {
            "range": range,
            "health": {
                "service": service_health,
                "database": database_health,
                "scheduler": scheduler_health,
                "telegram": telegram_health,
                "status": service_health,
                "schedulerRunning": service.scheduler.running,
                "uptimeSeconds": int(time.monotonic() - started_at),
                "runningTasks": running_tasks,
                "checkedAt": _iso(now),
            },
            "stats": stats,
            "statusBreakdown": status_breakdown,
            "upcomingTasks": [_upcoming_task_json(item, database) for item in upcoming],
            "accountStatus": [
                {"status": "active", "label": "正常", "count": active_accounts},
                {"status": "inactive", "label": "停用", "count": inactive_accounts},
            ],
            "accountSummary": {
                "total": len(account_items),
                "active": active_accounts,
                "inactive": inactive_accounts,
            },
            "recentRuns": [_run_json(item, database, service) for item in recent],
        }

    return router
