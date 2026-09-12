from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from tg_botx.core.time import utc_isoformat, utc_now
from tg_botx.features.bot.commands import BotCommandService
from tg_botx.features.bot.executors.base import (
    ERROR_MESSAGES,
    CommandContext,
    ExecutionError,
    ExecutionResult,
    json_bytes,
)
from tg_botx.features.bot.executors.registry import ExecutorRegistry
from tg_botx.features.bot.executors.schemas import code_hash
from tg_botx.infrastructure.persistence.db import Database
from tg_botx.infrastructure.persistence.models import BotCommandExecution
from tg_botx.integrations.python_runner import PythonRunnerClient

logger = logging.getLogger(__name__)
Sender = Callable[[int, str], Awaitable[None]]


class CommandAdmissionUnavailable(RuntimeError):
    """Transport must retry: durable command admission was not confirmed."""


def execution_view(item: BotCommandExecution, *, include_result: bool = True) -> dict[str, Any]:
    result = None
    if include_result and item.result_json:
        result = json.loads(item.result_json)
    return {
        "executionId": item.id,
        "command": item.command,
        "executorType": item.executor_type,
        "source": item.source,
        "status": item.status,
        "errorCode": item.error_code,
        "errorMessage": ERROR_MESSAGES.get(item.error_code or ""),
        "durationMs": item.duration_ms,
        "deliveryStatus": item.delivery_state,
        "deliveryAttempts": item.delivery_attempts,
        "createdAt": utc_isoformat(item.created_at),
        "startedAt": utc_isoformat(item.started_at),
        "finishedAt": utc_isoformat(item.finished_at),
        "result": result,
    }


class CommandExecutionService:
    def __init__(
        self,
        database: Database,
        registry: ExecutorRegistry,
        bot_identity: str,
        *,
        runner: PythonRunnerClient | None = None,
    ):
        self.database = database
        self.repository = database.command_executions
        self.registry = registry
        self.commands = BotCommandService(database, registry)
        self.bot_identity = bot_identity
        self.runner = runner
        self.sender: Sender | None = None
        self.owner = str(uuid.uuid4())
        self._tasks: list[asyncio.Task[None]] = []
        self._wake = asyncio.Event()
        self._closing = False
        self._started = False
        self._health_lock = asyncio.Lock()
        self._health_checked = 0.0

    async def start(self) -> None:
        if self._started:
            return
        if self._closing:
            raise ExecutionError("SERVICE_STOPPING")
        await self.refresh_capabilities(force=True)
        self._started = True
        self._tasks = [
            asyncio.create_task(self._worker(), name=f"command-worker-{index}")
            for index in range(self.registry.policy.max_workers)
        ]
        self._tasks.extend(
            asyncio.create_task(self._delivery_worker(), name=f"command-delivery-{index}")
            for index in range(2)
        )
        self._tasks.append(asyncio.create_task(self._maintenance(), name="command-maintenance"))

    async def refresh_capabilities(self, *, force: bool = False) -> None:
        if not self.registry.policy.python_enabled or self.runner is None:
            self.registry.python_available = False
            return
        async with self._health_lock:
            if force or time.monotonic() - self._health_checked > 5:
                self.registry.python_available = await self.runner.healthy()
                self._health_checked = time.monotonic()

    def _role(self, user_id: int | None, chat_id: int | None) -> str:
        if user_id is None or chat_id is None:
            return "anonymous"
        binding = self.database.get_bot_binding(user_id)
        return (
            (binding.role or "user")
            if binding is not None and binding.chat_id == chat_id
            else "anonymous"
        )

    def _require_ready(self, config: dict[str, Any], role: str) -> None:
        if not config.get("effectiveEnabled"):
            raise ExecutionError(config.get("executionErrorCode") or "EXECUTOR_UNAVAILABLE")
        if role not in config.get("effectiveAllowedRoles", config.get("allowedRoles", [])):
            raise ExecutionError("EXECUTION_FORBIDDEN")
        status = self.registry.status(
            config["executorType"],
            config["executorConfig"],
            config.get("codeHash") if config.get("codeConfirmed") else None,
        )
        if status.state != "ready":
            raise ExecutionError(status.code or "EXECUTOR_UNAVAILABLE")

    def submit(
        self,
        command: str,
        argument: str,
        *,
        user_id: int,
        chat_id: int,
        update_id: int,
    ) -> tuple[BotCommandExecution, bool]:
        config = next(
            (item for item in self.commands.command_configs() if item["command"] == command), None
        )
        if config is None or config.get("type") != "custom":
            raise ExecutionError("INVALID_EXECUTOR_CONFIG")
        role = self._role(user_id, chat_id)
        self._require_ready(config, role)
        return self._enqueue(
            config,
            argument,
            user_id=user_id,
            chat_id=chat_id,
            role=role,
            update_id=update_id,
            source="telegram",
            dedupe=f"update:{update_id}",
        )

    def submit_test(
        self,
        kind: str,
        config: dict[str, Any],
        argument: str,
        *,
        confirmation: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[BotCommandExecution, bool]:
        normalized = self.registry.normalize(kind, config)
        status = self.registry.status(kind, normalized, confirmation)
        if status.state != "ready":
            raise ExecutionError(status.code or "EXECUTOR_UNAVAILABLE")
        snapshot = {
            "command": "preview",
            "type": "custom",
            "executorType": kind,
            "executorConfig": normalized,
            "enabled": True,
            "effectiveEnabled": True,
            "allowedRoles": ["admin"],
            "revision": 0,
            "codeHash": code_hash(normalized) if kind == "python" else None,
            "codeConfirmed": bool(kind == "python" and confirmation == code_hash(normalized)),
        }
        key = idempotency_key or str(uuid.uuid4())
        return self._enqueue(
            snapshot,
            argument,
            user_id=None,
            chat_id=None,
            role="admin",
            update_id=None,
            source="test",
            dedupe="test:" + hashlib.sha256(key.encode()).hexdigest(),
        )

    def _enqueue(
        self,
        config: dict[str, Any],
        argument: str,
        *,
        user_id: int | None,
        chat_id: int | None,
        role: str,
        update_id: int | None,
        source: str,
        dedupe: str,
    ) -> tuple[BotCommandExecution, bool]:
        if self._closing or not self._started:
            raise ExecutionError("SERVICE_STOPPING")
        if not isinstance(argument, str) or len(argument.encode()) > 4096:
            raise ExecutionError("INVALID_EXECUTOR_CONFIG")
        now = utc_now()
        item = BotCommandExecution(
            id=str(uuid.uuid4()),
            bot_identity=self.bot_identity,
            dedupe_key=dedupe,
            command=config["command"],
            executor_type=config["executorType"],
            config_json=json_bytes(config, maximum=40 * 1024).decode(),
            revision=config["revision"],
            source=source,
            actor_key=f"telegram:{user_id}" if source == "telegram" else "admin-api",
            actor_role=role,
            user_id=user_id,
            chat_id=chat_id,
            update_id=update_id,
            argument=argument,
            created_at=now,
            expires_at=now + timedelta(seconds=self.registry.policy.queue_seconds),
            status="queued",
            delivery_state="pending" if source == "telegram" else "skipped",
        )
        try:
            stored, created = self.repository.enqueue(item, self.registry.policy)
        except SQLAlchemyError as exc:
            raise CommandAdmissionUnavailable("命令队列暂时不可用") from exc
        if (
            not created
            and source == "test"
            and (stored.config_json != item.config_json or stored.argument != argument)
        ):
            raise ExecutionError("IDEMPOTENCY_CONFLICT")
        self._wake.set()
        return stored, created

    async def _wait(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=0.5)
        self._wake.clear()

    async def _worker(self) -> None:
        while not self._closing:
            try:
                item = self.repository.claim(self.bot_identity, self.owner, self.registry.policy)
                if item is None:
                    await self._wait()
                else:
                    await self._execute(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("命令 worker 异常 type=%s", type(exc).__name__)
                await asyncio.sleep(1)

    async def _execute(self, item: BotCommandExecution) -> None:
        started = time.monotonic()
        result: str | None = None
        error: str | None = None
        status = "failed"
        try:
            config = json.loads(item.config_json)
            role = "admin" if item.source == "test" else self._role(item.user_id, item.chat_id)
            if item.source == "telegram":
                current = next(
                    (
                        row
                        for row in self.commands.command_configs()
                        if row["command"] == item.command
                    ),
                    None,
                )
                if current is None or current["revision"] != item.revision:
                    raise ExecutionError("CONFIG_CHANGED")
                config = current
            if item.executor_type == "python":
                await self.refresh_capabilities()
            self._require_ready(config, role)
            executor = self.registry.executors.get(item.executor_type)
            if executor is None:
                raise ExecutionError("EXECUTOR_UNAVAILABLE")
            context = CommandContext(
                item.id, item.command, item.argument, item.user_id, item.chat_id, role
            )
            async with asyncio.timeout(30):
                output = (await executor.execute(config["executorConfig"], context)).validated()
            result = json_bytes(output.payload()).decode()
            status = "succeeded"
        except ExecutionError as exc:
            error = exc.code
        except TimeoutError:
            error = "EXECUTION_TIMEOUT"
        except asyncio.CancelledError:
            status, error = "unknown", "EXECUTION_INTERRUPTED"
            raise
        except Exception as exc:
            error = "EXECUTION_FAILED"
            logger.warning(
                "自定义命令执行失败 execution_id=%s type=%s", item.id, type(exc).__name__
            )
        finally:
            self.repository.complete(
                item.id,
                self.owner,
                status=status,
                result_json=result,
                error_code=error,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self._wake.set()

    async def _delivery_worker(self) -> None:
        while not self._closing:
            try:
                if self.sender is None:
                    await self._wait()
                    continue
                item = self.repository.claim_delivery(self.bot_identity, self.owner)
                if item is None:
                    await self._wait()
                    continue
                success = False
                try:
                    text = (
                        ExecutionResult.from_payload(json.loads(item.result_json)).text
                        if item.result_json
                        else (
                            ERROR_MESSAGES.get(
                                item.error_code or "", ERROR_MESSAGES["EXECUTION_FAILED"]
                            )
                            + f"\n执行编号：{item.id}"
                        )
                    )
                    if item.chat_id is not None:
                        async with asyncio.timeout(15):
                            await self.sender(item.chat_id, text)
                        success = True
                finally:
                    self.repository.finish_delivery(item.id, self.owner, success=success)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("命令回复交付失败 type=%s", type(exc).__name__)
                await asyncio.sleep(1)

    async def _maintenance(self) -> None:
        while not self._closing:
            try:
                self.repository.prune(self.bot_identity, self.registry.policy.retention_days)
                await self.refresh_capabilities()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("命令维护失败 type=%s", type(exc).__name__)
            await asyncio.sleep(10)

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False
        await self.registry.close()
