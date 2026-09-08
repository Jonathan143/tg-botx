from __future__ import annotations

import asyncio
import copy
import json
from contextlib import suppress
from typing import Any

from tg_botx.features.checkin.errors import AccountNotFoundError as AccountNotFoundError
from tg_botx.features.checkin.errors import ManualRunConflict as ManualRunConflict
from tg_botx.features.checkin.errors import TaskNameConflictError as TaskNameConflictError
from tg_botx.features.checkin.errors import TaskNotFound as TaskNotFound
from tg_botx.features.checkin.errors import TaskStateError as TaskStateError
from tg_botx.features.checkin.errors import WorkflowVersionNotFound as WorkflowVersionNotFound
from tg_botx.features.checkin.notifications import NotificationService as NotificationService
from tg_botx.infrastructure.persistence.db import (
    Database,
    Task,
    TaskRun,
    utc_isoformat,
    utc_now,
)
from tg_botx.integrations.client_pool import ClientPool as ClientPool


class ProgressTracker:
    def __init__(self, database: Database):
        self.database = database
        self._task_event_sequence = 0
        self._task_subscribers: dict[str, set[asyncio.Queue[int]]] = {}
        self._task_run_progress: dict[str, dict[str, Any]] = {}

    def next_task_event_id(self) -> int:
        """Reserve a process-local, monotonically increasing task event ID."""

        self._task_event_sequence += 1
        return self._task_event_sequence

    def subscribe_task(self, task_id: str) -> asyncio.Queue[int]:
        """Subscribe to coalesced change notifications for one task."""

        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        self._task_subscribers.setdefault(task_id, set()).add(queue)
        return queue

    def unsubscribe_task(self, task_id: str, queue: asyncio.Queue[int]) -> None:
        subscribers = self._task_subscribers.get(task_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._task_subscribers.pop(task_id, None)

    def _publish_task_updated(self, task_id: str) -> None:
        event_id = self.next_task_event_id()
        for queue in tuple(self._task_subscribers.get(task_id, ())):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event_id)

    def get_task_run_progress(self, task_id: str) -> dict[str, Any] | None:
        progress = self._task_run_progress.get(task_id)
        return copy.deepcopy(progress) if progress is not None else None

    @staticmethod
    def _progress_step_statuses(progress: dict[str, object]) -> list[dict[str, object]]:
        statuses = progress.get("stepStatuses")
        if not isinstance(statuses, list):
            return []
        return [status for status in statuses if isinstance(status, dict)]

    @classmethod
    def _workflow_step_statuses(
        cls,
        steps: list[dict[str, object]],
        path_prefix: str = "steps",
        *,
        top_level: bool = True,
    ) -> list[dict[str, object]]:
        statuses: list[dict[str, object]] = []
        for index, step in enumerate(steps):
            path = f"{path_prefix}[{index}]"
            node_id = step.get("node_id") or step.get("nodeId")
            status: dict[str, object] = {"status": "pending"}
            if top_level:
                status["index"] = index
            if isinstance(node_id, str) and node_id:
                status["nodeId"] = node_id
                status["stepPath"] = path
            elif not top_level:
                status["stepPath"] = path
            statuses.append(status)
            if step.get("type") != "condition":
                continue
            branches = step.get("branches")
            if not isinstance(branches, list):
                continue
            for branch_index, branch in enumerate(branches):
                if not isinstance(branch, dict):
                    continue
                branch_steps = branch.get("steps")
                if not isinstance(branch_steps, list):
                    continue
                statuses.extend(
                    cls._workflow_step_statuses(
                        branch_steps,
                        f"{path}.branches[{branch_index}].steps",
                        top_level=False,
                    )
                )
        return statuses

    def _initialize_run_progress(
        self,
        task: Task,
        run: TaskRun,
        *,
        workflow_snapshot: dict[str, object] | None = None,
    ) -> None:
        progress = {
            "id": run.id,
            "status": "running",
            "attempt": 0,
            "stepStatuses": self._workflow_step_statuses(task.config["steps"]),
            "logs": [],
        }
        self._task_run_progress[task.id] = progress
        values: dict[str, object] = {"progress_json": json.dumps(progress, ensure_ascii=False)}
        if run.run_kind == "test" and workflow_snapshot is not None:
            values["workflow_json"] = json.dumps(workflow_snapshot, ensure_ascii=False)
        self.database.update_run(run.id, **values)

    def _persist_run_progress(self, task_id: str, run_id: str) -> None:
        progress = self._task_run_progress.get(task_id)
        if progress is None or progress["id"] != run_id:
            return
        self.database.update_run(
            run_id,
            progress_json=json.dumps(progress, ensure_ascii=False),
        )

    def _append_run_log(
        self,
        task_id: str,
        run_id: str,
        message: str,
        *,
        level: str = "INFO",
        step_index: int | None = None,
        node_id: str | None = None,
        step_path: str | None = None,
    ) -> None:
        progress = self._task_run_progress.get(task_id)
        if progress is None or progress["id"] != run_id:
            return
        logs = progress.setdefault("logs", [])
        if not isinstance(logs, list):
            return
        entry: dict[str, object] = {
            "timestamp": utc_isoformat(utc_now()),
            "level": level,
            "message": message,
            "stepIndex": step_index,
        }
        if node_id is not None:
            entry["nodeId"] = node_id
        if step_path is not None:
            entry["stepPath"] = step_path
        logs.append(entry)
        del logs[:-200]

    def _begin_run_attempt(self, task_id: str, run_id: str, attempt: int) -> None:
        progress = self._task_run_progress.get(task_id)
        if progress is None or progress["id"] != run_id:
            return
        progress["status"] = "running"
        progress["attempt"] = attempt
        progress.pop("error", None)
        for step in self._progress_step_statuses(progress):
            step["status"] = "pending"
            step.pop("error", None)
            step.pop("botResponse", None)
            step.pop("botButtons", None)
            step.pop("durationMs", None)
            step.pop("selectedBranch", None)
            step.pop("conditionVariables", None)
        self._append_run_log(task_id, run_id, f"开始第 {attempt} 次尝试")
        self._persist_run_progress(task_id, run_id)
        self._publish_task_updated(task_id)

    def _update_run_step(
        self,
        task_id: str,
        run_id: str,
        index: int | None,
        status: str,
        error: str | None = None,
        bot_response: str | None = None,
        bot_buttons: list[list[str]] | None = None,
        duration_ms: int | None = None,
        node_id: str | None = None,
        step_path: str | None = None,
        selected_branch: dict[str, object] | None = None,
        condition_variables: list[dict[str, object]] | None = None,
        include_condition_values: bool = False,
    ) -> None:
        progress = self._task_run_progress.get(task_id)
        if progress is None or progress["id"] != run_id:
            return
        steps = self._progress_step_statuses(progress)
        step = next(
            (
                item
                for item in steps
                if (node_id is not None and item.get("nodeId") == node_id)
                or (step_path is not None and item.get("stepPath") == step_path)
            ),
            None,
        )
        if step is None and index is not None:
            step = next((item for item in steps if item.get("index") == index), None)
        if step is None:
            return
        step["status"] = status
        if error is None:
            step.pop("error", None)
        else:
            step["error"] = error
        if bot_response is not None:
            step["botResponse"] = bot_response
        if bot_buttons is not None:
            step["botButtons"] = bot_buttons
        if duration_ms is not None and duration_ms > 0:
            step["durationMs"] = duration_ms
        if selected_branch is not None:
            step["selectedBranch"] = selected_branch
        if condition_variables is not None:
            values = copy.deepcopy(condition_variables)
            if not include_condition_values:
                for item in values:
                    item.pop("value", None)
            step["conditionVariables"] = values
        labels = {"running": "开始执行", "success": "执行成功", "failed": "执行失败"}
        self._append_run_log(
            task_id,
            run_id,
            error if status == "failed" and error else labels.get(status, status),
            level="ERROR" if status == "failed" else "INFO",
            step_index=index,
            node_id=node_id,
            step_path=step_path,
        )
        self._persist_run_progress(task_id, run_id)
        self._publish_task_updated(task_id)

    def _finalize_run_progress(
        self,
        task_id: str,
        run_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        progress = self._task_run_progress.get(task_id)
        if progress is None or progress["id"] != run_id:
            return
        progress["status"] = status
        if error is None:
            progress.pop("error", None)
        else:
            progress["error"] = error
        for step in self._progress_step_statuses(progress):
            if status == "success":
                if step["status"] == "running":
                    step["status"] = "success"
                    step.pop("error", None)
                elif step["status"] == "pending":
                    # Old integrations only report a final result. Preserve
                    # their top-level index behavior while keeping unvisited
                    # identity-aware branch nodes visibly skipped.
                    step["status"] = "skipped" if "stepPath" in step else "success"
            elif step["status"] == "running":
                step["status"] = "failed"
                step["error"] = error or "任务执行失败"
            elif step["status"] == "pending":
                step["status"] = "skipped"
                step.pop("error", None)
        self._append_run_log(
            task_id,
            run_id,
            error if error else f"运行{status}",
            level="ERROR" if status == "failed" else "INFO",
        )
        self._persist_run_progress(task_id, run_id)
