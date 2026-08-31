from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tg_botx.config import Settings
from tg_botx.infrastructure.persistence.db import Account, Database, TaskRun, utc_now
from tg_botx.features.checkin.runtime import CheckinService, ManualRunConflict
from tg_botx.schemas import TaskDefinition


def definition(
    name: str,
    *,
    target: str = "checkin_bot",
    at: str = "23:59:00",
) -> TaskDefinition:
    return TaskDefinition.model_validate(
        {
            "name": name,
            "account": "default",
            "target": target,
            "schedule": {"type": "fixed", "timezone": "UTC", "time": at},
            "steps": [{"type": "send_message", "text": "/checkin"}],
        }
    )


def resources(tmp_path):
    settings = Settings(data_dir=tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'database.sqlite3'}")
    database.create_all()
    database.save_account(Account(name="default", session_name="default"))
    return settings, database


def create_published_task(service: CheckinService, definition: TaskDefinition):
    task = service.create_task(definition)
    service.publish_task(task.id)
    return task


def test_database_admin_queries_and_dashboard_stats(tmp_path):
    settings, database = resources(tmp_path)
    service = CheckinService(settings, database)
    first = create_published_task(service, definition("alpha"))
    second = create_published_task(service, definition("beta", target="another_bot"))
    service.enable_task(first.id)
    service.archive_task(second.id)

    database.add_run(
        TaskRun(
            task_id=first.id,
            status="success",
            started_at=utc_now() - timedelta(minutes=5),
            finished_at=utc_now(),
        )
    )
    database.add_run(
        TaskRun(
            task_id=first.id,
            status="failed",
            started_at=utc_now() - timedelta(days=2),
            finished_at=utc_now() - timedelta(days=2),
        )
    )

    items, total = database.list_tasks_page(search="alp", page_size=1000)
    assert total == 1
    assert [item.name for item in items] == ["alpha"]
    archived, archived_total = database.list_tasks_page(include_archived=True)
    assert archived_total == 2
    assert {item.name for item in archived} == {"alpha", "beta"}

    runs, run_total = database.list_runs(task_id=first.id, status="success")
    assert run_total == 1
    assert runs[0].status == "success"
    stats = database.dashboard_stats(utc_now() - timedelta(hours=24))
    assert stats["tasks_total"] == 2
    assert stats["tasks_enabled"] == 1
    assert stats["tasks_archived"] == 1
    assert stats["runs_total"] == 1
    assert stats["runs_success"] == 1


def test_task_mutations_synchronize_scheduler(tmp_path):
    async def scenario():
        settings, database = resources(tmp_path)
        service = CheckinService(settings, database)
        await service.start()
        try:
            task = create_published_task(service, definition("daily"))
            assert task.enabled is False
            assert task.next_run_at is None
            assert service.scheduler.get_job(f"task:{task.id}") is None

            enabled = service.enable_task(task.id)
            first_next = enabled.next_run_at
            assert first_next is not None
            assert service.scheduler.get_job(f"task:{task.id}") is not None

            edited = service.edit_task(task.id, definition("daily", at="01:23:45"))
            assert edited.next_run_at is not None
            assert edited.next_run_at != first_next
            job = service.scheduler.get_job(f"task:{task.id}")
            assert job is not None
            assert job.next_run_time == edited.next_run_at

            disabled = service.disable_task(task.id)
            assert disabled.next_run_at is None
            assert service.scheduler.get_job(f"task:{task.id}") is None

            service.enable_task(task.id)
            archived = service.archive_task(task.id)
            assert archived.archived is True
            assert archived.enabled is False
            assert archived.next_run_at is None
            assert service.scheduler.get_job(f"task:{task.id}") is None

            restored = service.restore_task(task.id)
            assert restored.archived is False
            assert restored.enabled is False
            assert restored.next_run_at is None
        finally:
            await service.close()

    asyncio.run(scenario())


def test_manual_busy_conflict_does_not_record_skipped(tmp_path, monkeypatch):
    async def scenario():
        settings, database = resources(tmp_path)
        service = CheckinService(settings, database)
        first = create_published_task(service, definition("first"))
        second = create_published_task(service, definition("second"))
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run_with_retries(executor, task):
            started.set()
            await release.wait()
            return True, None, 1, "ok"

        async def fake_get(account):
            return object()

        monkeypatch.setattr(
            "tg_botx.features.checkin.runtime.run_with_retries", fake_run_with_retries
        )
        monkeypatch.setattr(service.pool, "get", fake_get)
        await service.start()
        try:
            run_id = service.start_manual_run(first.id)
            await started.wait()
            with pytest.raises(ManualRunConflict):
                service.start_manual_run(second.id)

            current_runs, total = database.list_runs()
            assert total == 1
            assert current_runs[0].id == run_id
            assert all(item.status != "skipped" for item in current_runs)

            running = service.running[first.id]
            release.set()
            assert await running is True
            assert database.get_run(run_id).status == "success"
        finally:
            await service.close()

    asyncio.run(scenario())


def test_task_update_subscription_tracks_manual_run_state(tmp_path, monkeypatch):
    async def scenario():
        settings, database = resources(tmp_path)
        service = CheckinService(settings, database)
        task = create_published_task(service, definition("events"))
        queue = service.subscribe_task(task.id)
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run_with_retries(executor, snapshot):
            started.set()
            await release.wait()
            return True, None, 1, "ok"

        async def fake_get(account):
            return object()

        monkeypatch.setattr(
            "tg_botx.features.checkin.runtime.run_with_retries", fake_run_with_retries
        )
        monkeypatch.setattr(service.pool, "get", fake_get)
        await service.start()
        try:
            service.start_manual_run(task.id)
            await started.wait()
            started_event_id = queue.get_nowait()
            assert task.id in service.running
            assert database.has_running_run(task.id) is True

            running = service.running[task.id]
            release.set()
            assert await running is True
            finished_event_id = queue.get_nowait()
            current = database.get_task(task.id)
            assert finished_event_id > started_event_id
            assert task.id not in service.running
            assert database.has_running_run(task.id) is False
            assert current.last_status == "success"
            assert current.last_run_at is not None
            assert current.updated_at >= current.last_run_at
            progress = service.get_task_run_progress(task.id)
            assert progress["status"] == "success"
            assert progress["stepStatuses"] == [{"index": 0, "status": "success"}]
        finally:
            service.unsubscribe_task(task.id, queue)
            assert service._task_subscribers == {}
            await service.close()

    asyncio.run(scenario())


def test_step_progress_is_published_by_index_and_retries_reset_attempt(tmp_path):
    async def scenario():
        settings, database = resources(tmp_path)
        service = CheckinService(settings, database)
        task = create_published_task(
            service,
            TaskDefinition.model_validate(
                {
                    "name": "step-events",
                    "account": "default",
                    "target": "checkin_bot",
                    "schedule": {
                        "type": "fixed",
                        "timezone": "UTC",
                        "time": "23:59:00",
                    },
                    "retry": {"max_attempts": 2, "backoff_seconds": [0]},
                    "steps": [
                        {"type": "send_message", "text": "/start"},
                        {"type": "send_message", "text": "/finish"},
                    ],
                }
            ),
        )
        first_attempt_failed = asyncio.Event()
        second_step_started = asyncio.Event()
        release_second_step = asyncio.Event()

        class FakeClient:
            send_count = 0

            async def get_entity(self, target):
                return SimpleNamespace(id=1, bot=True)

            async def get_messages(self, entity, **kwargs):
                return []

            async def send_message(self, entity, text):
                self.send_count += 1
                if self.send_count == 1:
                    first_attempt_failed.set()
                    raise RuntimeError("首次发送失败")
                if text == "/finish":
                    second_step_started.set()
                    await release_second_step.wait()
                return SimpleNamespace(id=self.send_count)

        fake_client = FakeClient()

        async def fake_get(account):
            return fake_client

        service.pool.get = fake_get
        await service.start()
        try:
            service.start_manual_run(task.id)
            await first_attempt_failed.wait()
            await second_step_started.wait()
            progress = service.get_task_run_progress(task.id)
            assert progress["attempt"] == 2
            assert progress["status"] == "running"
            assert progress["stepStatuses"] == [
                {"index": 0, "status": "success"},
                {"index": 1, "status": "running"},
            ]

            running = service.running[task.id]
            release_second_step.set()
            assert await running is True
            progress = service.get_task_run_progress(task.id)
            assert progress["status"] == "success"
            assert progress["attempt"] == 2
            assert progress["stepStatuses"] == [
                {"index": 0, "status": "success"},
                {"index": 1, "status": "success"},
            ]
        finally:
            await service.close()

    asyncio.run(scenario())


def test_cancel_marks_running_step_failed_and_remaining_steps_skipped(tmp_path):
    async def scenario():
        settings, database = resources(tmp_path)
        service = CheckinService(settings, database)
        task = create_published_task(
            service,
            TaskDefinition.model_validate(
                {
                    "name": "cancel-steps",
                    "account": "default",
                    "target": "checkin_bot",
                    "schedule": {
                        "type": "fixed",
                        "timezone": "UTC",
                        "time": "23:59:00",
                    },
                    "steps": [
                        {"type": "send_message", "text": "/start"},
                        {"type": "send_message", "text": "/never"},
                    ],
                }
            ),
        )
        step_started = asyncio.Event()

        class FakeClient:
            async def get_entity(self, target):
                return SimpleNamespace(id=1, bot=True)

            async def get_messages(self, entity, **kwargs):
                return []

            async def send_message(self, entity, text):
                step_started.set()
                await asyncio.Event().wait()

        async def fake_get(account):
            return FakeClient()

        service.pool.get = fake_get
        await service.start()
        try:
            service.start_manual_run(task.id)
            await step_started.wait()
            running = service.running[task.id]
            assert await service.cancel_task(task.id) is True
            assert await running is False

            progress = service.get_task_run_progress(task.id)
            assert progress["status"] == "canceled"
            assert progress["error"] == "收到取消请求"
            step_statuses = progress["stepStatuses"]
            assert [
                {key: value for key, value in item.items() if key != "durationMs"}
                for item in step_statuses
            ] == [
                {"index": 0, "status": "failed", "error": "任务已取消"},
                {"index": 1, "status": "skipped"},
            ]
            if "durationMs" in step_statuses[0]:
                assert step_statuses[0]["durationMs"] >= 0
            assert database.get_task(task.id).last_status == "canceled"
        finally:
            await service.close()

    asyncio.run(scenario())


def test_failed_run_keeps_failed_step_error_and_skips_later_steps(tmp_path):
    async def scenario():
        settings, database = resources(tmp_path)
        service = CheckinService(settings, database)
        task = create_published_task(
            service,
            TaskDefinition.model_validate(
                {
                    "name": "failed-steps",
                    "account": "default",
                    "target": "checkin_bot",
                    "schedule": {
                        "type": "fixed",
                        "timezone": "UTC",
                        "time": "23:59:00",
                    },
                    "retry": {"max_attempts": 1, "backoff_seconds": []},
                    "steps": [
                        {"type": "send_message", "text": "/fail"},
                        {"type": "send_message", "text": "/never"},
                    ],
                }
            ),
        )

        class FakeClient:
            async def get_entity(self, target):
                return SimpleNamespace(id=1, bot=True)

            async def get_messages(self, entity, **kwargs):
                return []

            async def send_message(self, entity, text):
                raise RuntimeError("发送失败")

        async def fake_get(account):
            return FakeClient()

        service.pool.get = fake_get
        await service.start()
        try:
            service.start_manual_run(task.id)
            running = service.running[task.id]
            assert await running is False

            progress = service.get_task_run_progress(task.id)
            assert progress["status"] == "failed"
            assert progress["attempt"] == 1
            assert progress["stepStatuses"] == [
                {
                    "index": 0,
                    "status": "failed",
                    "error": "步骤 1 执行失败：发送失败",
                },
                {"index": 1, "status": "skipped"},
            ]
            assert database.get_task(task.id).last_status == "failed"
        finally:
            await service.close()

    asyncio.run(scenario())


def test_cancel_before_execution_starts_still_finalizes_task_and_steps(tmp_path):
    async def scenario():
        settings, database = resources(tmp_path)
        service = CheckinService(settings, database)
        task = create_published_task(service, definition("cancel-before-start"))
        await service.start()
        try:
            service.start_manual_run(task.id)
            running = service.running[task.id]
            assert await service.cancel_task(task.id) is True
            with pytest.raises(asyncio.CancelledError):
                await running
            await asyncio.sleep(0)

            current = database.get_task(task.id)
            progress = service.get_task_run_progress(task.id)
            assert current.last_status == "canceled"
            assert current.last_run_at is not None
            assert progress["status"] == "canceled"
            assert progress["stepStatuses"] == [{"index": 0, "status": "skipped"}]
        finally:
            await service.close()

    asyncio.run(scenario())


def test_running_snapshot_cannot_overwrite_edit_or_disable(tmp_path, monkeypatch):
    async def scenario():
        settings, database = resources(tmp_path)
        service = CheckinService(settings, database)
        task = create_published_task(service, definition("snapshot", at="23:59:00"))
        service.enable_task(task.id)
        started = asyncio.Event()
        release = asyncio.Event()
        seen_times: list[str | None] = []

        async def fake_run_with_retries(executor, snapshot):
            seen_times.append(snapshot.fixed_time)
            started.set()
            await release.wait()
            return True, None, 1, None

        async def fake_get(account):
            return object()

        monkeypatch.setattr(
            "tg_botx.features.checkin.runtime.run_with_retries", fake_run_with_retries
        )
        monkeypatch.setattr(service.pool, "get", fake_get)
        await service.start()
        try:
            execution = asyncio.create_task(service.run_task(task.id))
            await started.wait()
            edited = service.edit_task(task.id, definition("snapshot", at="01:02:03"))
            edited_next = edited.next_run_at
            service.disable_task(task.id)
            release.set()
            assert await execution is True

            current = database.get_task(task.id)
            assert seen_times == ["23:59:00"]
            assert current.enabled is False
            assert current.next_run_at is None
            assert service.scheduler.get_job(f"task:{task.id}") is None
            assert edited_next is not None
        finally:
            await service.close()

    asyncio.run(scenario())
