"""Fixed-policy Docker workload manager, never an in-process Python sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import shutil
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tg_botx.features.bot.executors.base import ExecutionError, ExecutionResult, json_bytes
from tg_botx.features.bot.executors.schemas import PythonConfig

LABEL = "tg-botx.python-runner"


class RunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TG_BOT_RUNNER_", extra="ignore", hide_input_in_errors=True
    )
    token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    host: str = "127.0.0.1"
    port: int = Field(default=8766, ge=1, le=65535)
    image: str = "tg-botx-python-sandbox:1"
    docker_binary: str = "docker"
    docker_host: str = "unix:///var/run/docker.sock"
    instance: str = Field(default="default", pattern=r"^[a-z0-9_-]{1,24}$")
    concurrency: int = Field(default=2, ge=1, le=8)
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None

    @model_validator(mode="after")
    def check_settings(self) -> RunnerSettings:
        if len(self.token.get_secret_value().encode()) < 32:
            raise ValueError("Runner token 至少需要 32 字节")
        if not self.docker_host.startswith("unix://"):
            raise ValueError("Runner 仅连接部署端的 Unix Docker socket")
        if (
            not self.image
            or self.image.startswith("-")
            or any(char.isspace() for char in self.image)
        ):
            raise ValueError("Runner 镜像引用无效")
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise ValueError("TLS 证书与私钥必须同时配置")
        return self


class RunnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    execution_id: str = Field(alias="executionId")
    code: str = Field(min_length=1, max_length=24_000)
    context: dict[str, Any]
    timeout_seconds: int = Field(default=3, alias="timeoutSeconds", ge=1, le=10)

    @model_validator(mode="after")
    def check_request(self) -> RunnerRequest:
        try:
            if str(UUID(self.execution_id)) != self.execution_id:
                raise ValueError("noncanonical UUID")
            PythonConfig(code=self.code, timeoutSeconds=self.timeout_seconds)
            json_bytes(self.model_dump(by_alias=True))
        except (ValueError, ExecutionError) as exc:
            raise ValueError("执行请求格式无效") from exc
        return self


class DockerSandbox:
    def __init__(self, settings: RunnerSettings):
        self.settings = settings
        self.binary = shutil.which(settings.docker_binary)
        self.image_id: str | None = None
        self.ready = False
        self.active: set[str] = set()
        self.cancel_requested: set[str] = set()
        self.quarantined = False
        self.orphaned: set[str] = set()
        self.completed: dict[str, float] = {}
        self.slots = asyncio.Semaphore(settings.concurrency)
        # Do not inherit Bot secrets, registry credentials, proxy settings or .env.
        self.environment = {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "DOCKER_HOST": settings.docker_host,
        }

    async def command(
        self, *args: str, data: bytes | None = None, timeout: float = 5, maximum: int = 64 * 1024
    ) -> tuple[int, bytes]:
        if self.binary is None:
            raise ExecutionError("EXECUTOR_UNAVAILABLE")
        process = await asyncio.create_subprocess_exec(
            self.binary,
            *args,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.environment,
            start_new_session=True,
        )

        async def read(stream: asyncio.StreamReader | None) -> bytes:
            assert stream is not None
            value = bytearray()
            while chunk := await stream.read(4096):
                value.extend(chunk)
                if len(value) > maximum:
                    raise ExecutionError("OUTPUT_TOO_LARGE")
            return bytes(value)

        async def write() -> None:
            if data is not None and process.stdin is not None:
                try:
                    process.stdin.write(data)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()

        try:
            async with asyncio.timeout(timeout):
                async with asyncio.TaskGroup() as group:
                    stdout = group.create_task(read(process.stdout))
                    group.create_task(read(process.stderr))
                    group.create_task(write())
                    group.create_task(process.wait())
                return process.returncode or 0, stdout.result()
        except TimeoutError as exc:
            raise ExecutionError("EXECUTION_TIMEOUT") from exc
        except ExceptionGroup as exc:
            if any(
                isinstance(item, ExecutionError) and item.code == "OUTPUT_TOO_LARGE"
                for item in exc.exceptions
            ):
                raise ExecutionError("OUTPUT_TOO_LARGE") from exc
            raise ExecutionError("EXECUTION_FAILED") from exc
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    def container_name(self, execution_id: str) -> str:
        return f"tgx-python-{self.settings.instance}-{UUID(execution_id).hex}"

    async def probe(self) -> bool:
        try:
            status, content = await self.command("info", "--format", "{{json .}}")
            info = json.loads(content)
            if (
                status
                or not isinstance(info, dict)
                or info.get("OSType") != "linux"
                # Docker's JSON API uses CpuCfsQuota, not the Go field CPUCfsQuota.
                or not all(
                    info.get(key) is True
                    for key in (
                        "MemoryLimit",
                        "SwapLimit",
                        "PidsLimit",
                        "CpuCfsPeriod",
                        "CpuCfsQuota",
                    )
                )
            ):
                raise ExecutionError("EXECUTOR_UNAVAILABLE")
            if not any("seccomp" in item for item in info.get("SecurityOptions", [])):
                raise ExecutionError("EXECUTOR_UNAVAILABLE")
            status, content = await self.command(
                "image", "inspect", "--format", "{{.Id}}", self.settings.image
            )
            image = content.decode().strip()
            if status or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise ExecutionError("EXECUTOR_UNAVAILABLE")
            # Pin the local immutable image ID, not a tag that can change mid-run.
            self.image_id = image
            self.ready = not self.quarantined
        except (ExecutionError, ValueError, OSError, TypeError):
            self.ready = False
        return self.ready

    def create_arguments(self, execution_id: str, seconds: int) -> list[str]:
        assert self.image_id is not None
        return [
            "create",
            "--name",
            self.container_name(execution_id),
            "--label",
            f"{LABEL}={self.settings.instance}",
            "--label",
            f"{LABEL}.expires={int(time.time()) + seconds + 15}",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "16",
            "--memory",
            "128m",
            "--memory-swap",
            "128m",
            "--cpus",
            "0.5",
            "--ulimit",
            "nofile=64:64",
            "--ulimit",
            "fsize=1048576:1048576",
            "--ulimit",
            "cpu=12:12",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=8388608",
            "--workdir",
            "/tmp",
            "--log-driver",
            "none",
            "--init",
            "--interactive",
            "--entrypoint",
            "python",
            self.image_id,
            "-I",
            "-B",
            "/runner/entrypoint.py",
        ]

    def remember_finished(self, execution_id: str) -> None:
        self.completed[execution_id] = time.monotonic()
        while len(self.completed) > 1024:
            self.completed.pop(next(iter(self.completed)))

    async def remove(self, execution_id: str) -> None:
        try:
            status, _ = await self.command("rm", "--force", self.container_name(execution_id))
            if status:
                # A failed inspect is ambiguous (missing vs disconnected daemon).
                # Only a successful empty listing proves the workload is absent.
                checked, remaining = await self.command(
                    "ps",
                    "--all",
                    "--filter",
                    f"name=^/{self.container_name(execution_id)}$",
                    "--format",
                    "{{.Names}}",
                )
                if checked or remaining.strip():
                    raise ExecutionError("EXECUTOR_UNAVAILABLE")
            self.orphaned.discard(execution_id)
        except (ExecutionError, OSError):
            self.ready = False
            self.quarantined = True
            self.orphaned.add(execution_id)
            raise

    async def execute(self, request: RunnerRequest) -> ExecutionResult:
        if not self.ready or self.image_id is None:
            raise ExecutionError("EXECUTOR_UNAVAILABLE")
        if self.slots.locked():
            raise ExecutionError("EXECUTION_BUSY")
        if request.execution_id in self.active or request.execution_id in self.completed:
            raise ExecutionError("EXECUTION_FORBIDDEN")
        await self.slots.acquire()
        self.active.add(request.execution_id)
        try:
            # Independent server-side deadline even if the caller disconnects.
            async with asyncio.timeout(request.timeout_seconds + 10):
                status, _ = await self.command(
                    *self.create_arguments(request.execution_id, request.timeout_seconds)
                )
                if status:
                    raise ExecutionError("EXECUTOR_UNAVAILABLE")
                if request.execution_id in self.cancel_requested:
                    raise ExecutionError("EXECUTION_INTERRUPTED")
                status, output = await self.command(
                    "start",
                    "--attach",
                    "--interactive",
                    self.container_name(request.execution_id),
                    data=json_bytes({"code": request.code, "context": request.context}),
                    timeout=request.timeout_seconds,
                    maximum=16 * 1024,
                )
                if status:
                    raise ExecutionError("EXECUTION_FAILED")
                try:
                    return ExecutionResult.from_payload(json.loads(output))
                except (ValueError, TypeError) as exc:
                    raise ExecutionError("EXECUTION_FAILED") from exc
        except TimeoutError as exc:
            raise ExecutionError("EXECUTION_TIMEOUT") from exc
        finally:
            cleanup = asyncio.create_task(self.remove(request.execution_id))
            try:
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
            finally:
                self.active.discard(request.execution_id)
                self.cancel_requested.discard(request.execution_id)
                self.remember_finished(request.execution_id)
                # Only replay prevention for the live manager. The Bot's database
                # provides durable dedupe and never retries uncertain executions.
                self.slots.release()

    async def sweep(self, *, startup: bool = False) -> None:
        cleanup_failed = False
        for execution_id in tuple(self.orphaned):
            try:
                await self.remove(execution_id)
            except (ExecutionError, OSError):
                cleanup_failed = True
        status, content = await self.command(
            "ps",
            "--all",
            "--filter",
            f"label={LABEL}={self.settings.instance}",
            "--format",
            "{{.ID}}",
        )
        if status:
            self.ready = False
            self.quarantined = True
            return
        for container in content.decode().splitlines():
            if not re.fullmatch(r"[0-9a-f]{12,64}", container):
                continue
            status, labels = await self.command(
                "inspect", "--format", "{{json .Config.Labels}}", container
            )
            if status:
                cleanup_failed = True
                continue
            try:
                expired = int(json.loads(labels).get(f"{LABEL}.expires", "0")) < time.time()
            except (ValueError, TypeError, AttributeError):
                expired = True
            if startup or expired:
                status, _ = await self.command("rm", "--force", container)
                if status:
                    self.ready = False
                    cleanup_failed = True
        self.quarantined = cleanup_failed

    async def maintenance(self) -> None:
        while True:
            try:
                await self.sweep()
                await self.probe()
            except (ExecutionError, OSError):
                self.ready = False
            await asyncio.sleep(5)


def create_runner_app(
    settings: RunnerSettings | None = None, *, sandbox: DockerSandbox | None = None
) -> FastAPI:
    resolved = settings if settings is not None else RunnerSettings()
    manager = sandbox if sandbox is not None else DockerSandbox(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await manager.probe()
        if manager.ready:
            await manager.sweep(startup=True)
        task = asyncio.create_task(manager.maintenance())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            for execution_id in tuple(manager.active):
                with contextlib.suppress(ExecutionError):
                    await manager.remove(execution_id)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.sandbox = manager

    @app.middleware("http")
    async def guard(request: Request, call_next):
        expected = ("Bearer " + resolved.token.get_secret_value()).encode()
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected):
            return JSONResponse({"errorCode": "UNAUTHORIZED"}, status_code=401)
        if request.method == "POST":
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 32 * 1024:
                    return JSONResponse({"errorCode": "OUTPUT_TOO_LARGE"}, status_code=413)
            request._body = bytes(raw)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"errorCode": "INVALID_EXECUTOR_CONFIG"}, status_code=422)

    @app.exception_handler(ExecutionError)
    async def error_handler(_: Request, exc: ExecutionError) -> JSONResponse:
        status = (
            503
            if exc.code == "EXECUTOR_UNAVAILABLE"
            else 429
            if exc.code == "EXECUTION_BUSY"
            else 422
        )
        return JSONResponse({"errorCode": exc.code}, status_code=status)

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {"ready": manager.ready, "protocolVersion": 1, "isolated": True}

    @app.post("/v1/executions")
    async def execute(body: RunnerRequest) -> dict[str, Any]:
        return (await manager.execute(body)).payload()

    @app.delete("/v1/executions/{execution_id}")
    async def cancel(execution_id: str) -> dict[str, bool]:
        try:
            UUID(execution_id)
        except ValueError as exc:
            raise ExecutionError("INVALID_EXECUTOR_CONFIG") from exc
        # Cancel arriving before a delayed POST must prevent that POST from starting.
        manager.remember_finished(execution_id)
        if execution_id in manager.active:
            manager.cancel_requested.add(execution_id)
            await manager.remove(execution_id)
        return {"cancelled": True}

    return app
