import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from tg_botx.core.time import utc_now
from tg_botx.features.bot.execution import CommandExecutionService, execution_view
from tg_botx.features.bot.executors.base import ExecutionError
from tg_botx.features.bot.executors.builtin import BuiltinFunctionExecutor
from tg_botx.features.bot.executors.policy import ExecutorPolicy
from tg_botx.features.bot.executors.registry import ExecutorRegistry
from tg_botx.infrastructure.persistence.models import BotAuditLog, BotBinding, BotCommandExecution

ECHO = {"function": "echo", "arguments": {"text": "{{ argument }}"}}


def item(update_id, *, actor="user", executor="builtin_function", bot="bot:test"):
    return BotCommandExecution(
        id=str(uuid4()),
        bot_identity=bot,
        dedupe_key=f"update:{update_id}",
        command="custom",
        executor_type=executor,
        config_json="{}",
        revision=1,
        actor_key=actor,
        actor_role="user",
        argument="input-not-in-audit",
        user_id=123,
        chat_id=123,
        update_id=update_id,
        source="telegram",
        status="queued",
        created_at=utc_now(),
        expires_at=utc_now() + timedelta(seconds=60),
    )


def test_atomic_dedupe_and_global_queue_limit(db):
    policy = ExecutorPolicy(queue_limit=2, rate_limit=100)
    repo = db.command_executions

    def insert(_):
        return repo.enqueue(item(1), policy)[1]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(insert, range(8))) == 1
    repo.enqueue(item(2, actor="other"), policy)
    with pytest.raises(ExecutionError) as error:
        repo.enqueue(item(3, actor="third"), policy)
    assert error.value.code == "EXECUTION_BUSY"
    assert not repo.enqueue(item(1), policy)[1]  # Duplicates don't consume capacity.
    repo.enqueue(item(1, bot="bot:other"), policy)  # Different bot identities are independent.


def test_user_rate_limit(db):
    repo = db.command_executions
    policy = ExecutorPolicy(rate_limit=1)
    repo.enqueue(item(1), policy)
    with pytest.raises(ExecutionError) as error:
        repo.enqueue(item(2), policy)
    assert error.value.code == "EXECUTION_RATE_LIMITED"


def test_global_user_and_python_concurrency_across_workers(db):
    repo = db.command_executions
    policy = ExecutorPolicy(max_workers=2, python_workers=1, rate_limit=100)
    for row in [
        item(1, executor="python"),
        item(2, actor="user"),
        item(3, actor="python-other", executor="python"),
        item(4, actor="unrelated"),
    ]:
        repo.enqueue(row, policy)
    first = repo.claim("bot:test", "worker-one", policy)
    second = repo.claim("bot:test", "worker-two", policy)
    assert first.update_id == 1 and second.update_id == 4
    assert repo.claim("bot:test", "worker-three", policy) is None
    assert (
        repo.complete(
            first.id,
            "wrong-owner",
            status="succeeded",
            result_json='{"text":"bad"}',
            error_code=None,
            duration_ms=1,
        )
        is False
    )
    assert repo.complete(
        first.id,
        "worker-one",
        status="succeeded",
        result_json='{"text":"ok"}',
        error_code=None,
        duration_ms=1,
    )
    assert repo.claim("bot:test", "worker-three", policy).update_id == 2


def test_crash_recovery_never_reexecutes_unknown_side_effects_and_expires_queue(db):
    repo, policy = db.command_executions, ExecutorPolicy(rate_limit=100)
    one, _ = repo.enqueue(item(1), policy)
    two, _ = repo.enqueue(item(2), policy)
    claimed = repo.claim("bot:test", "crashed", policy)
    with db.session() as session:
        session.execute(
            update(BotCommandExecution)
            .where(BotCommandExecution.id == claimed.id)
            .values(lease_until=utc_now() - timedelta(seconds=1))
        )
        session.execute(
            update(BotCommandExecution)
            .where(BotCommandExecution.id == two.id)
            .values(expires_at=utc_now() - timedelta(seconds=1))
        )
        session.commit()
    assert repo.claim("bot:test", "new-worker", policy) is None
    assert repo.get(one.id, "bot:test").status == "unknown"
    assert repo.get(two.id, "bot:test").error_code == "QUEUE_EXPIRED"
    assert repo.get(one.id, "bot:other") is None


def test_execution_and_delivery_are_separate_with_bounded_retries(db):
    repo, policy = db.command_executions, ExecutorPolicy()
    queued, _ = repo.enqueue(item(1), policy)
    row = repo.claim("bot:test", "worker", policy)
    repo.complete(
        row.id,
        "worker",
        status="succeeded",
        result_json='{"text":"<unsafe>& plain text"}',
        error_code=None,
        duration_ms=5,
    )
    for attempt in range(3):
        reply = repo.claim_delivery("bot:test", "delivery")
        assert reply.delivery_attempts == attempt + 1
        repo.finish_delivery(row.id, "delivery", success=False)
        with db.session() as session:
            session.execute(
                update(BotCommandExecution)
                .where(BotCommandExecution.id == row.id)
                .values(delivery_after=utc_now() - timedelta(seconds=1))
            )
            session.commit()
        assert repo.claim("bot:test", "execution-again", policy) is None
    assert repo.claim_delivery("bot:test", "delivery") is None
    saved = repo.get(queued.id, "bot:test")
    assert saved.status == "succeeded" and saved.delivery_state == "failed"
    assert "argument" not in execution_view(saved) and "config" not in execution_view(saved)
    with db.session() as session:
        logs = list(session.scalars(select(BotAuditLog)))
        assert len(logs) == 1 and "input-not-in-audit" not in logs[0].details


def service_for(db, *, executor=None):
    registry = ExecutorRegistry(ExecutorPolicy(max_workers=2, rate_limit=100))
    registry.executors["builtin_function"] = executor or BuiltinFunctionExecutor(db)
    service = CommandExecutionService(db, registry, "bot:live")
    service.commands.create_command_config(
        "hello", "示例", enabled=True, executor_type="builtin_function", executor_config=ECHO
    )
    return service


async def await_terminal(service, execution_id):
    async with asyncio.timeout(5):
        while True:
            row = service.repository.get(execution_id, service.bot_identity)
            if row.status not in {"queued", "running"}:
                return row
            await asyncio.sleep(0.01)


async def test_service_executes_dedupes_and_sends_plain_result(db):
    service = service_for(db)
    replies = []

    async def sender(chat, text):
        replies.append((chat, text))

    service.sender = sender
    await service.start()
    try:
        first, created = service.submit(
            "hello", "<b>not markup</b>", user_id=123, chat_id=123, update_id=1
        )
        repeated, created_again = service.submit(
            "hello", "<b>not markup</b>", user_id=123, chat_id=123, update_id=1
        )
        assert created and not created_again and repeated.id == first.id
        result = await await_terminal(service, first.id)
        assert result.status == "succeeded"
        async with asyncio.timeout(5):
            while not replies:
                await asyncio.sleep(0.01)
        assert replies == [(123, "<b>not markup</b>")]
    finally:
        await service.close()
    assert not service._tasks


async def test_config_changed_between_admission_and_execution_is_cancelled(db):
    service = service_for(db)
    service._started = True  # Drive claim/execute deterministically instead of racing workers.
    queued, _ = service.submit("hello", "input", user_id=123, chat_id=123, update_id=1)
    service.commands.patch_command_config("hello", {"enabled": False})
    claimed = service.repository.claim(service.bot_identity, service.owner, service.registry.policy)
    await service._execute(claimed)
    assert service.repository.get(queued.id, service.bot_identity).error_code == "CONFIG_CHANGED"
    await service.close()


async def test_builtin_identity_is_trusted_context_not_arguments(db, context):
    executor = BuiltinFunctionExecutor(db)
    with pytest.raises(ExecutionError):
        await executor.execute({"function": "system_status"}, context)
    with pytest.raises(ExecutionError):
        await executor.execute({"function": "my_points"}, context)
    with db.session() as session:
        session.add(BotBinding(user_id=123, chat_id=123, role="user"))
        session.commit()
    result = await executor.execute({"function": "my_points"}, context)
    assert result.data == {"points": 0}
    assert (await executor.execute({"function": "utc_time"}, context)).text.endswith("Z")


async def test_test_idempotency_rejects_changed_payload_and_no_role_forgery(db):
    service = service_for(db)
    await service.start()
    try:
        row, _ = service.submit_test("builtin_function", ECHO, "first", idempotency_key="stable")
        with pytest.raises(ExecutionError) as error:
            service.submit_test("builtin_function", ECHO, "changed", idempotency_key="stable")
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
        assert (await await_terminal(service, row.id)).status == "succeeded"
        identity, _ = service.submit_test(
            "builtin_function", {"function": "my_points"}, "", idempotency_key="me"
        )
        assert (await await_terminal(service, identity.id)).error_code == "EXECUTION_FORBIDDEN"
    finally:
        await service.close()


async def test_shutdown_marks_running_work_unknown_and_cancels_executor(db):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class BlockedExecutor:
        async def execute(self, config, context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def close(self):
            pass

    service = service_for(db, executor=BlockedExecutor())
    await service.start()
    row, _ = service.submit("hello", "input", user_id=123, chat_id=123, update_id=1)
    await asyncio.wait_for(entered.wait(), 5)
    await service.close()
    assert cancelled.is_set()
    assert service.repository.get(row.id, service.bot_identity).status == "unknown"
