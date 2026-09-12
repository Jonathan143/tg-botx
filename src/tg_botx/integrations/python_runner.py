from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
from typing import Any

import httpx

from tg_botx.features.bot.executors.base import (
    MAX_RESULT_BYTES,
    CommandContext,
    ExecutionError,
    ExecutionResult,
    json_bytes,
)
from tg_botx.features.bot.executors.schemas import PythonConfig, parsed_url


def validate_runner_url(value: str) -> str:
    url = parsed_url(value)
    if url.path not in {"", "/"} or url.query:
        raise ValueError("Runner URL 不能包含路径或查询参数")
    if url.scheme != "https":
        try:
            loopback = ipaddress.ip_address(url.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("远程 Runner 必须使用 HTTPS；仅数字 loopback 地址可使用 HTTP")
    return value.rstrip("/")


class PythonRunnerClient:
    def __init__(self, url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.url = validate_runner_url(url)
        if len(token.encode()) < 32:
            raise ValueError("Runner token 至少需要 32 字节")
        self.client = httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            follow_redirects=False,
            timeout=3,
            transport=transport,
        )

    async def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None, timeout: float = 3
    ) -> tuple[int, dict[str, Any]]:
        async with asyncio.timeout(timeout):
            async with self.client.stream(
                method,
                path,
                content=json_bytes(payload) if payload is not None else None,
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
                timeout=timeout,
            ) as response:
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ExecutionError("EXECUTOR_UNAVAILABLE")
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESULT_BYTES:
                        raise ExecutionError("OUTPUT_TOO_LARGE")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ExecutionError("EXECUTOR_UNAVAILABLE")
                return response.status_code, value

    async def healthy(self) -> bool:
        try:
            status, data = await self._request("GET", "/v1/health")
            return (
                status == 200
                and data.get("ready") is True
                and data.get("isolated") is True
                and data.get("protocolVersion") == 1
            )
        except (httpx.HTTPError, TimeoutError, ValueError, ExecutionError):
            return False

    async def execute(self, config: dict[str, Any], context: CommandContext) -> ExecutionResult:
        parsed = PythonConfig.model_validate(config)
        try:
            status, data = await self._request(
                "POST",
                "/v1/executions",
                payload={
                    "executionId": context.execution_id,
                    "code": parsed.code,
                    "context": context.payload(),
                    "timeoutSeconds": parsed.timeout_seconds,
                },
                timeout=parsed.timeout_seconds + 15,
            )
            if status != 200:
                code = data.get("errorCode")
                raise ExecutionError(code if isinstance(code, str) else "EXECUTOR_UNAVAILABLE")
            return ExecutionResult.from_payload(data)
        except (TimeoutError, httpx.TimeoutException) as exc:
            await self.cancel(context.execution_id)
            raise ExecutionError("EXECUTION_TIMEOUT") from exc
        except asyncio.CancelledError:
            await self.cancel(context.execution_id)
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionError("EXECUTOR_UNAVAILABLE") from exc

    async def cancel(self, execution_id: str) -> None:
        with contextlib.suppress(httpx.HTTPError, TimeoutError, ValueError, ExecutionError):
            await self._request("DELETE", f"/v1/executions/{execution_id}")

    async def close(self) -> None:
        await self.client.aclose()
