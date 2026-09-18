"""受控 HTTP 出站：在建立每个 TCP 连接时校验并固定 DNS 结果。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import zlib
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpcore
import httpx

from tg_botx.features.bot.executors.base import ExecutionError, json_bytes
from tg_botx.features.bot.executors.policy import ExecutorPolicy
from tg_botx.features.bot.executors.policy import public_ip as public_ip
from tg_botx.features.bot.executors.schemas import HttpConfig, parsed_url, validate_headers

MAX_RESPONSE_BYTES = 64 * 1024
SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)


class BoundedResolver:
    """Cancellation cannot kill libc DNS; bound outstanding resolver threads instead."""

    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="command-dns")
        self.slots = asyncio.Semaphore(4)

    async def resolve(self, host: str, port: int) -> list[str]:
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        if self.slots.locked():
            raise ExecutionError("EXECUTION_BUSY")
        await self.slots.acquire()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self.pool, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        )
        future.add_done_callback(lambda _: self.slots.release())
        try:
            records = await asyncio.shield(future)
        except OSError as exc:
            raise ExecutionError("HTTP_REQUEST_FAILED") from exc
        return list(dict.fromkeys(str(record[4][0]) for record in records))

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        *,
        backend: httpcore.AsyncNetworkBackend | None = None,
        resolver: Callable[[str, int], Awaitable[list[str]]] | None = None,
    ) -> None:
        self.backend = backend if backend is not None else httpcore.AnyIOBackend()
        self.owned_resolver = BoundedResolver() if resolver is None else None
        self.resolve = resolver if resolver is not None else self.owned_resolver.resolve  # type: ignore[union-attr]

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self.resolve(host, port)
        if not addresses or len(addresses) > 16 or any(not public_ip(item) for item in addresses):
            raise ExecutionError("HTTP_TARGET_BLOCKED")
        # Only a validated numeric address reaches the socket layer. httpcore keeps
        # the original request host for TLS SNI and certificate verification.
        return await self.backend.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise ExecutionError("HTTP_TARGET_BLOCKED")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def close(self) -> None:
        if self.owned_resolver is not None:
            self.owned_resolver.close()


class SafeHttpClient:
    def __init__(
        self,
        policy: ExecutorPolicy,
        *,
        backend: PublicNetworkBackend | None = None,
    ) -> None:
        self.policy = policy
        self.backend = backend if backend is not None else PublicNetworkBackend()
        self.pool = httpcore.AsyncConnectionPool(
            network_backend=self.backend,
            ssl_context=httpcore.default_ssl_context(),
            max_connections=policy.max_workers,
            max_keepalive_connections=0,
            http2=False,
            retries=0,
        )

    async def request(
        self,
        config: HttpConfig,
        *,
        query: dict[str, str],
        headers: dict[str, str],
        json_body: Any = None,
        text_body: str | None = None,
    ) -> bytes:
        url = parsed_url(config.url)
        if not self.policy.allows_url(config.url):
            raise ExecutionError("HTTP_TARGET_BLOCKED")
        # Literal hosts get the same policy as DNS answers, including IPv6.
        try:
            ipaddress.ip_address(url.host)
        except ValueError:
            pass
        else:
            if not public_ip(url.host):
                raise ExecutionError("HTTP_TARGET_BLOCKED")
        try:
            validate_headers(headers)
        except ValueError as exc:
            raise ExecutionError("INVALID_EXECUTOR_CONFIG") from exc
        headers = dict(headers)
        if config.credential_ref:
            credential = self.policy.credentials.get(config.credential_ref)
            if credential is None or not self.policy.allows_url(credential.origin):
                raise ExecutionError("HTTP_TARGET_BLOCKED")
            from tg_botx.features.bot.executors.schemas import origin

            if origin(credential.origin) != origin(config.url):
                raise ExecutionError("HTTP_TARGET_BLOCKED")
            lower = {name.casefold() for name in credential.headers}
            headers = {
                name: value for name, value in headers.items() if name.casefold() not in lower
            }
            headers.update(
                {name: value.get_secret_value() for name, value in credential.headers.items()}
            )
        headers["Accept-Encoding"] = "identity"
        content: bytes | None = None
        if json_body is not None:
            content = json_bytes(json_body)
            if not any(name.casefold() == "content-type" for name in headers):
                headers["Content-Type"] = "application/json"
        elif text_body is not None:
            content = text_body.encode()
            if len(content) > 32 * 1024:
                raise ExecutionError("INVALID_EXECUTOR_CONFIG")
        request = httpx.Request(
            config.method, url.copy_merge_params(query), headers=headers, content=content
        )
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.content,
            extensions={
                "timeout": {
                    name: config.timeout_seconds for name in ("connect", "read", "write", "pool")
                }
            },
        )
        try:
            async with asyncio.timeout(config.timeout_seconds):
                response = await self.pool.handle_async_request(core_request)
                try:
                    # No redirect following, including same-origin redirects.
                    if not 200 <= response.status < 300:
                        raise ExecutionError("HTTP_REQUEST_FAILED")
                    return await self._read(response)
                finally:
                    await response.aclose()
        except (TimeoutError, httpcore.TimeoutException) as exc:
            raise ExecutionError("EXECUTION_TIMEOUT") from exc
        except (httpcore.NetworkError, httpcore.ProtocolError, OSError) as exc:
            raise ExecutionError("HTTP_REQUEST_FAILED") from exc

    @staticmethod
    async def _read(response: httpcore.Response) -> bytes:
        headers = {name.lower(): value for name, value in response.headers}
        length = headers.get(b"content-length")
        if length is not None:
            try:
                if int(length) < 0 or int(length) > MAX_RESPONSE_BYTES:
                    raise ExecutionError("OUTPUT_TOO_LARGE")
            except ValueError as exc:
                raise ExecutionError("HTTP_RESPONSE_INVALID") from exc
        encoding = headers.get(b"content-encoding", b"identity").lower().strip()
        if encoding not in {b"identity", b"gzip", b"deflate"}:
            raise ExecutionError("HTTP_RESPONSE_INVALID")
        decoder = (
            None
            if encoding == b"identity"
            else zlib.decompressobj(31 if encoding == b"gzip" else 15)
        )
        raw_count = 0
        output = bytearray()
        try:
            async for chunk in response.aiter_stream():
                raw_count += len(chunk)
                if raw_count > MAX_RESPONSE_BYTES:
                    raise ExecutionError("OUTPUT_TOO_LARGE")
                data = (
                    decoder.decompress(chunk, MAX_RESPONSE_BYTES - len(output) + 1)
                    if decoder
                    else chunk
                )
                output.extend(data)
                if len(output) > MAX_RESPONSE_BYTES or (decoder and decoder.unconsumed_tail):
                    raise ExecutionError("OUTPUT_TOO_LARGE")
            if decoder and (not decoder.eof or decoder.unused_data):
                raise ExecutionError("HTTP_RESPONSE_INVALID")
        except zlib.error as exc:
            raise ExecutionError("HTTP_RESPONSE_INVALID") from exc
        return bytes(output)

    async def close(self) -> None:
        try:
            await self.pool.aclose()
        finally:
            self.backend.close()


def extract_text(payload: bytes, config: HttpConfig) -> str:
    try:
        text = payload.decode("utf-8")
        if config.response.type == "text":
            return text
        value = json.loads(text)
        for part in (config.response.text_path or "").split("."):
            if isinstance(value, dict):
                value = value[part]
            elif isinstance(value, list) and part.isdecimal():
                value = value[int(part)]
            else:
                raise ValueError("invalid path")
        if not isinstance(value, str):
            raise ValueError("response text must be a string")
        return value
    except (ValueError, KeyError, IndexError, UnicodeError, RecursionError) as exc:
        raise ExecutionError("HTTP_RESPONSE_INVALID") from exc
