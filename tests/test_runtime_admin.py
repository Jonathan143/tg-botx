from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tg_bot.config import Settings
from tg_bot.db import Account, Database, TaskRun, utc_now
from tg_bot.runtime import CheckinService, ManualRunConflict
from tg_bot.schemas import TaskDefinition


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


def test_database_admin_queries_and_dashboard_stats(tmp_path):
    settings, database = resources(tmp_path)
    service = CheckinService(settings, database)
    first = service.create_task(definition("alpha"))
    second = service.create_task(definition("beta", target="another_bot"))
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
            task = service.create_task(definition("daily"))
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
        first = service.create_task(definition("first"))
        second = service.create_task(definition("second"))
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run_with_retries(executor, task):
            started.set()
            await release.wait()
            return True, None, 1, "ok"

        async def fake_get(account):
            return object()

        monkeypatch.setattr("tg_bot.runtime.run_with_retries", fake_run_with_retries)
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


def test_running_snapshot_cannot_overwrite_edit_or_disable(tmp_path, monkeypatch):
    async def scenario():
        settings, database = resources(tmp_path)
        service = CheckinService(settings, database)
        task = service.create_task(definition("snapshot", at="23:59:00"))
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

        monkeypatch.setattr("tg_bot.runtime.run_with_retries", fake_run_with_retries)
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
