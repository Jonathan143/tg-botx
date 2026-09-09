"""Validated, intentionally limited executors for management-Bot commands.

Python and JavaScript are deliberately not interpreted here. A command
configuration is persisted by an administrator and can be triggered by a
Telegram user, so evaluating arbitrary source in the service process would be
an unsafe privilege boundary. If those executors are needed later they must
be implemented behind a separately isolated worker.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx


class CommandExecutionError(RuntimeError):
    """A safe, user-facing error raised while executing a custom command."""


class CommandConfigError(CommandExecutionError, ValueError):
    """A command executor configuration cannot be accepted or executed."""


_BUILTIN_NAMES = ("echo", "uppercase")
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH"}
_HTTP_ALLOWED_KEYS = {"url", "method", "headers", "timeout", "retries"}
_HTTP_RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_MAX_REPLY_LENGTH = 4000
_MAX_URL_LENGTH = 2048
_MAX_HEADERS = 50
_MAX_HEADER_NAME_LENGTH = 128
_MAX_HEADER_VALUE_LENGTH = 4096
_MAX_RETRIES = 3
_FORBIDDEN_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.google.com",
}


def _echo(argument: str, _: dict[str, Any]) -> str:
    return argument or "请输入要回显的内容。"


def _uppercase(argument: str, _: dict[str, Any]) -> str:
    return argument.upper()


BUILTINS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "echo": _echo,
    "uppercase": _uppercase,
}


def validate_executor_config(
    executor_type: str,
    config: object,
    *,
    enabled: bool = True,
    allow_disabled_legacy: bool = False,
) -> None:
    """Validate an executor before it is persisted or enabled.

    The API accepts a JSON object, but this function also protects direct
    service callers and old rows loaded from storage. ``allow_disabled_legacy``
    exists only to let an administrator turn off an old Python/JavaScript row;
    those rows remain non-executable and are never enabled by this function.
    """

    if not isinstance(config, dict):
        raise CommandConfigError("执行器配置必须是 JSON 对象")

    if executor_type in {"python", "javascript"}:
        if allow_disabled_legacy and not enabled:
            return
        raise CommandConfigError(
            f"{executor_type} 执行器暂未开放；请使用 http 或 builtin_function，"
            "或等待隔离执行环境上线"
        )
    if executor_type == "none":
        if config:
            raise CommandConfigError("none 执行器不能配置参数")
        if enabled:
            raise CommandConfigError("未配置可执行器，不能启用该自定义指令")
        return
    if executor_type == "builtin_function":
        unknown = set(config) - {"name"}
        if unknown:
            raise CommandConfigError(
                f"builtin_function 包含不支持的配置项：{', '.join(sorted(unknown))}"
            )
        name = config.get("name")
        if not isinstance(name, str) or name not in BUILTINS:
            raise CommandConfigError(
                f"未注册的内置函数，可用值：{', '.join(_BUILTIN_NAMES)}"
            )
        return
    if executor_type == "http":
        _validate_http_config(config)
        return
    raise CommandConfigError("不支持的指令执行器")


def executor_config_error(executor_type: str, config: object, *, enabled: bool) -> str | None:
    """Return a stable configuration error for legacy rows shown by the API."""

    try:
        validate_executor_config(executor_type, config, enabled=enabled)
    except CommandConfigError as exc:
        return str(exc)
    return None


def _validate_http_config(config: dict[str, Any]) -> None:
    unknown = set(config) - _HTTP_ALLOWED_KEYS
    if unknown:
        raise CommandConfigError(
            f"http 执行器包含不支持的配置项：{', '.join(sorted(unknown))}"
        )

    url = config.get("url")
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LENGTH:
        raise CommandConfigError("HTTP 执行器需要不超过 2048 个字符的 URL")
    _parse_public_url(url)

    method = config.get("method", "POST")
    if not isinstance(method, str) or method.upper() not in _HTTP_METHODS:
        raise CommandConfigError("HTTP 方法仅支持 GET、POST、PUT、PATCH")

    timeout = config.get("timeout", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise CommandConfigError("HTTP 超时必须是 1 至 30 秒的数字")
    if not 1 <= timeout <= 30:
        raise CommandConfigError("HTTP 超时必须在 1 至 30 秒之间")

    retries = config.get("retries", 0)
    if (
        isinstance(retries, bool)
        or not isinstance(retries, int)
        or not 0 <= retries <= _MAX_RETRIES
    ):
        raise CommandConfigError(f"HTTP retries 必须是 0 至 {_MAX_RETRIES} 的整数")

    headers = config.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > _MAX_HEADERS:
        raise CommandConfigError(
            f"HTTP headers 必须是最多 {_MAX_HEADERS} 项的字符串键值对象"
        )
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not name
            or len(name) > _MAX_HEADER_NAME_LENGTH
            or len(value) > _MAX_HEADER_VALUE_LENGTH
            or any(char in name or char in value for char in ("\r", "\n"))
        ):
            raise CommandConfigError("HTTP headers 必须是合法的字符串键值对象")


def _parse_public_url(url: str):
    if any(char.isspace() for char in url):
        raise CommandConfigError("HTTP URL 不能包含空白字符")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise CommandConfigError("HTTP 执行器仅支持 http/https URL")
    if parsed.username is not None or parsed.password is not None:
        raise CommandConfigError("HTTP URL 不允许携带用户名或密码")
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise CommandConfigError("HTTP URL 的主机或端口无效") from exc
    if not hostname:
        raise CommandConfigError("HTTP URL 必须包含主机名")
    if port is not None and not 1 <= port <= 65535:
        raise CommandConfigError("HTTP URL 端口必须在 1 至 65535 之间")
    if _is_private_address(hostname):
        raise CommandConfigError("HTTP 目标地址不能是本机、内网或保留地址")
    return parsed, hostname, port or (443 if parsed.scheme.lower() == "https" else 80)


def _is_private_address(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized in _FORBIDDEN_HOSTNAMES or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return not address.is_global


async def execute_command(executor_type: str, config: dict[str, Any], argument: str) -> str:
    """Execute one command using only the explicitly supported executors."""

    try:
        validate_executor_config(executor_type, config, enabled=True)
    except CommandConfigError as exc:
        raise CommandExecutionError(str(exc)) from exc

    if executor_type == "builtin_function":
        return BUILTINS[config["name"]](argument, config)
    if executor_type == "http":
        return await _execute_http(config, argument)
    # ``none`` and the disabled languages are rejected by validation above.
    raise CommandExecutionError("该自定义指令未配置可执行器")


async def _execute_http(config: dict[str, Any], argument: str) -> str:
    url = config["url"]
    method = config.get("method", "POST").upper()
    timeout = float(config.get("timeout", 10))
    retries = config.get("retries", 0)
    headers = config.get("headers", {})
    await _ensure_public_dns_target(url)

    payload = {"argument": argument}
    last_error: Exception | None = None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False
    ) as client:
        for attempt in range(retries + 1):
            try:
                response = await client.request(method, url, headers=headers, json=payload)
                if response.status_code in _HTTP_RETRY_STATUSES and attempt < retries:
                    await asyncio.sleep(0.1 * (attempt + 1))
                    continue
                response.raise_for_status()
                text = response.text.strip()
                return text[:_MAX_REPLY_LENGTH] or "HTTP 执行成功。"
            except httpx.HTTPStatusError as exc:
                raise CommandExecutionError(
                    f"HTTP 执行失败：远端返回 HTTP {exc.response.status_code}"
                ) from exc
            except httpx.RequestError as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(0.1 * (attempt + 1))
                    continue
                break
    error_name = type(last_error).__name__ if last_error else "请求错误"
    raise CommandExecutionError(f"HTTP 执行失败：{error_name}") from last_error


async def _ensure_public_dns_target(url: str) -> None:
    """Resolve hostnames before connecting so private targets cannot be used."""

    _, hostname, port = _parse_public_url(url)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.run_in_executor(
                None, socket.getaddrinfo, hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except OSError as exc:
            raise CommandExecutionError("HTTP 目标地址无法解析") from exc
        addresses = {str(info[4][0]) for info in infos if info and info[4]}
        if not addresses or any(_is_private_address(address) for address in addresses):
            raise CommandExecutionError(
                "HTTP 目标地址不能解析到本机、内网或保留地址"
            ) from None
