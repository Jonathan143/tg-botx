from __future__ import annotations

import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from tg_botx.features.checkin.runtime import (
    CheckinService,
)
from tg_botx.features.checkin.schedule import schedule_from_task
from tg_botx.infrastructure.observability.logging import redact_sensitive
from tg_botx.infrastructure.persistence.db import (
    Database,
    Task,
    TaskRun,
    WorkflowVersion,
    utc_isoformat,
)
from tg_botx.schemas import TaskDefinition

logger = logging.getLogger(__name__)


def _iso(value: datetime | str | None) -> str | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        return utc_isoformat(parsed)
    return utc_isoformat(value)


def _redact_step_buttons(step: dict[str, Any]) -> None:
    buttons = step.get("botButtons")
    if not isinstance(buttons, list):
        return
    for row in buttons:
        if not isinstance(row, list):
            continue
        for index, label in enumerate(row):
            if isinstance(label, str):
                row[index] = redact_sensitive(label)


def _task_json(task: Task, database: Database, service: CheckinService) -> dict[str, Any]:
    account = database.get_account_by_id(task.account_id)
    run = service.get_task_run_progress(task.id)
    if run is not None:
        if isinstance(run.get("error"), str):
            run["error"] = redact_sensitive(run["error"])
        for step in run["stepStatuses"]:
            if isinstance(step.get("error"), str):
                step["error"] = redact_sensitive(step["error"])
            if isinstance(step.get("botResponse"), str):
                step["botResponse"] = redact_sensitive(step["botResponse"])
            _redact_step_buttons(step)
        for log in run.get("logs", []):
            if isinstance(log.get("message"), str):
                log["message"] = redact_sensitive(log["message"])
    schedule = schedule_from_task(task).model_dump(mode="json", exclude_none=True)
    versions = database.list_workflow_versions(task.id)
    latest_version = versions[0] if versions else None
    return {
        "id": task.id,
        "name": task.name,
        "account": account.name if account else None,
        "accountId": task.account_id,
        "target": task.target,
        "timezone": task.timezone,
        "schedule": schedule,
        "definition": TaskDefinition.model_validate(task.config).to_api_dict(),
        "enabled": task.enabled,
        "archived": task.archived,
        "running": task.id in service.running or database.has_running_run(task.id),
        "nextRunAt": _iso(task.next_run_at),
        "lastRunAt": _iso(task.last_run_at),
        "lastStatus": task.last_status,
        "run": run,
        "createdAt": _iso(task.created_at),
        "updatedAt": _iso(task.updated_at),
        "latestWorkflowVersion": latest_version.version_number if latest_version else None,
        "workflowVersions": [
            {
                "id": item.id,
                "version": item.version_number,
                "publishedAt": _iso(item.published_at),
                "releaseNote": item.release_note,
            }
            for item in versions
        ],
    }


def _run_json(
    run: TaskRun,
    database: Database,
    service: CheckinService | None = None,
    *,
    include_workflow: bool = True,
    include_progress_logs: bool = True,
) -> dict[str, Any]:
    task = database.get_task_any(run.task_id)
    progress = service.get_task_run_progress(run.task_id) if service and task else None
    if progress is not None and progress.get("id") != run.id:
        progress = None
    if progress is None and run.progress_json:
        try:
            stored_progress = json.loads(run.progress_json)
            if isinstance(stored_progress, dict) and stored_progress.get("id") == run.id:
                progress = stored_progress
        except (TypeError, json.JSONDecodeError):
            progress = None
    if progress is not None:
        progress = copy.deepcopy(
            {
                key: value
                for key, value in progress.items()
                if include_progress_logs or key != "logs"
            }
        )
        if isinstance(progress.get("error"), str):
            progress["error"] = redact_sensitive(progress["error"])
        for step in progress.get("stepStatuses", []):
            if isinstance(step.get("error"), str):
                step["error"] = redact_sensitive(step["error"])
            if isinstance(step.get("botResponse"), str):
                step["botResponse"] = redact_sensitive(step["botResponse"])
            _redact_step_buttons(step)
        if include_progress_logs:
            for log in progress.get("logs", []):
                if isinstance(log.get("message"), str):
                    log["message"] = redact_sensitive(log["message"])
    result = {
        "id": run.id,
        "taskId": run.task_id,
        "taskName": task.name if task else None,
        "timezone": task.timezone if task else "UTC",
        "plannedAt": _iso(run.planned_at),
        "startedAt": _iso(run.started_at),
        "finishedAt": _iso(run.finished_at),
        "status": run.status,
        "attempts": run.attempts,
        "error": redact_sensitive(run.error) if run.error else None,
        "runKind": run.run_kind,
        "workflowVersion": (
            int(run.workflow_version)
            if run.workflow_version and run.workflow_version.isdigit()
            else run.workflow_version
        ),
        "workflowVersionId": run.workflow_version_id,
        "progress": progress,
    }
    if include_workflow:
        workflow: dict[str, Any] | None = None
        if run.run_kind == "test" and run.workflow_json:
            try:
                stored_workflow = json.loads(run.workflow_json)
                if isinstance(stored_workflow, dict):
                    workflow = stored_workflow
            except (TypeError, json.JSONDecodeError):
                workflow = None
        version: WorkflowVersion | None = None
        if run.workflow_version_id:
            version = database.get_workflow_version(run.workflow_version_id)
        if workflow is None and version is not None:
            workflow = version.execution_definition
        workflow_error = None
        if run.run_kind == "published" and workflow is None:
            workflow_error = "发布版本数据不存在"
        result["workflow"] = workflow
        result["workflowError"] = workflow_error
    return result


def _flow_json(flow: Any) -> dict[str, Any]:
    return {
        "flowId": flow.flow_id,
        "accountName": flow.account_name,
        "accountId": flow.account_id,
        "method": flow.method,
        "stage": flow.stage,
        "qrUrl": flow.qr_url,
        "qrExpiresAt": _iso(flow.qr_expires_at),
        "createdAt": _iso(flow.created_at),
        "updatedAt": _iso(flow.updated_at),
    }


def _account_json(account: Any, database: Database) -> dict[str, Any]:
    if hasattr(account, "account_id"):
        summary = database.account_task_summary(account.account_id)
        return {
            "id": account.account_id,
            "name": account.name,
            "phoneMasked": account.phone_masked,
            "active": account.is_active,
            "enabledTaskCount": summary["enabled"],
            "taskCount": summary["total"],
            "createdAt": _iso(account.created_at),
        }
    return {
        "id": account.id,
        "name": account.name,
        "phoneMasked": None,
        "active": account.is_active,
        "enabledTaskCount": 0,
        "taskCount": 0,
        "createdAt": _iso(account.created_at),
    }


def _dashboard_trend(
    database: Database,
    selected_range: Literal["24h", "7d", "30d"],
    now: datetime,
) -> tuple[datetime, list[dict[str, Any]]]:
    now = now.astimezone(UTC)
    if selected_range == "24h":
        bucket_count = 24
        bucket_width = timedelta(hours=1)
        first_bucket = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)

        def label(value):
            return utc_isoformat(value) or ""
    else:
        bucket_count = 7 if selected_range == "7d" else 30
        bucket_width = timedelta(days=1)
        first_bucket = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=bucket_count - 1
        )

        def label(value):
            return value.date().isoformat()

    buckets: list[dict[str, Any]] = [
        {"label": label(first_bucket + index * bucket_width), "success": 0, "failed": 0}
        for index in range(bucket_count)
    ]
    width_seconds = bucket_width.total_seconds()
    for started_at, status in database.dashboard_run_events(first_bucket):
        if status not in {"success", "failed"}:
            continue
        normalized = (
            started_at.replace(tzinfo=UTC)
            if started_at.tzinfo is None
            else started_at.astimezone(UTC)
        )
        index = int((normalized - first_bucket).total_seconds() // width_seconds)
        if 0 <= index < bucket_count:
            buckets[index][status] += 1
    return first_bucket, buckets


def _upcoming_task_json(task: Task, database: Database) -> dict[str, Any]:
    account = database.get_account_by_id(task.account_id)
    return {
        "id": task.id,
        "name": task.name,
        "account": account.name if account else None,
        "accountId": task.account_id,
        "target": task.target,
        "timezone": task.timezone,
        "nextRunAt": _iso(task.next_run_at),
    }
