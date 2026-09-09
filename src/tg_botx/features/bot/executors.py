"""Safe executors for admin Bot custom commands."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx


class CommandExecutionError(RuntimeError):
    pass


def _echo(argument: str, _: dict[str, Any]) -> str:
    return argument or "请输入要回显的内容。"


def _uppercase(argument: str, _: dict[str, Any]) -> str:
    return argument.upper()


BUILTINS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "echo": _echo,
    "uppercase": _uppercase,
}


async def execute_command(executor_type: str, config: dict[str, Any], argument: str) -> str:
    if executor_type == "builtin_function":
        name = config.get("name")
        function = BUILTINS.get(name) if isinstance(name, str) else None
        if function is None:
            raise CommandExecutionError("未注册的内置函数，可用值：echo、uppercase")
        return function(argument, config)
    if executor_type == "http":
        return await _execute_http(config, argument)
    if executor_type in {"python", "javascript"}:
        raise CommandExecutionError("Python/JavaScript 执行器默认禁用，需配置隔离运行环境")
    raise CommandExecutionError("该自定义指令未配置可执行器")


async def _execute_http(config: dict[str, Any], argument: str) -> str:
    url = config.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise CommandExecutionError("HTTP 执行器需要合法的 http/https URL")
    method = str(config.get("method", "POST")).upper()
    if method not in {"GET", "POST", "PUT", "PATCH"}:
        raise CommandExecutionError("HTTP 方法仅支持 GET、POST、PUT、PATCH")
    timeout = float(config.get("timeout", 10))
    if not 1 <= timeout <= 30:
        raise CommandExecutionError("HTTP 超时必须在 1 至 30 秒之间")
    headers = config.get("headers", {})
    if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        raise CommandExecutionError("HTTP headers 必须是字符串键值对象")
    payload = {"argument": argument}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.request(method, url, headers=headers, json=payload)
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise CommandExecutionError(f"HTTP 执行失败：{type(exc).__name__}") from exc
    text = response.text.strip()
    return text[:4000] or "HTTP 执行成功。"
