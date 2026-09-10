"""Safe, explicit executors for administrator-defined Bot commands.

The management bot deliberately does not dispatch arbitrary callables from a
database row.  Every executor is selected from an allow-list and receives a
small, normalized command context.  Python and JavaScript are opt-in and run
in short-lived subprocesses with a reduced runtime, a temporary working
directory and operating-system resource limits.
"""

from __future__ import annotations

import ast
import asyncio
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_HTTP_TIMEOUT_SECONDS = 30
MAX_SCRIPT_TIMEOUT_SECONDS = 5
MAX_HTTP_RETRIES = 3
MAX_HTTP_RESPONSE_BYTES = 64 * 1024
MAX_SCRIPT_BYTES = 16 * 1024
MAX_SCRIPT_OUTPUT_CHARS = 12 * 1024
MAX_HTTP_OUTPUT_CHARS = 12 * 1024

_COMMAND_VARIABLE = re.compile(r"{{\s*([A-Za-z][A-Za-z0-9]*)\s*}}")
_PRIVATE_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
_SCRIPT_FORBIDDEN_NAMES = {
    "__import__",
    "breakpoint",
    "builtins",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "hasattr",
    "input",
    "locals",
    "object",
    "open",
    "os",
    "pathlib",
    "setattr",
    "shutil",
    "socket",
    "subprocess",
    "sys",
    "type",
}


class CustomCommandExecutorError(RuntimeError):
    """An expected, user-facing executor failure."""


class CustomCommandConfigError(ValueError):
    """A malformed or unsafe executor configuration."""


@dataclass(frozen=True, slots=True)
class CustomCommandContext:
    command: str
    argument: str
    text: str
    user_id: int
    chat_id: int

    @property
    def args(self) -> list[str]:
        # Telegram arguments are intentionally split using shell-like quoting;
        # this keeps a quoted value intact without executing a shell.
        import shlex

        try:
            return shlex.split(self.argument)
        except ValueError as exc:
            raise CustomCommandExecutorError("命令参数的引号格式无效") from exc

    def payload(self) -> dict[str, Any]:
        args = self.args
        return {
            "command": self.command,
            "argument": self.argument,
            "args": args,
            "text": self.text,
            "userId": self.user_id,
            "chatId": self.chat_id,
        }

    def variables(self) -> dict[str, str]:
        args = self.args
        return {
            "command": self.command,
            "args": self.argument,
            "text": self.text,
            "userId": str(self.user_id),
            "chatId": str(self.chat_id),
            "argsJson": json.dumps(args, ensure_ascii=False),
            **{f"arg{index}": value for index, value in enumerate(args)},
        }


BUILTIN_FUNCTIONS = ("args", "echo", "json", "utc_time")


def validate_executor_config(
    executor_type: str, config: object, *, enabled: bool = False
) -> dict[str, Any]:
    """Validate and normalize a persisted executor configuration.

    The function is shared by the HTTP API and runtime so a command cannot be
    saved in a shape that the Telegram path cannot execute.
    """

    if executor_type not in {"none", "http", "builtin_function", "python", "javascript"}:
        raise CustomCommandConfigError("不支持的指令执行器")
    if not isinstance(config, dict):
        raise CustomCommandConfigError("执行器配置必须是 JSON 对象")
    value = dict(config)
    if executor_type == "none":
        if enabled:
            raise CustomCommandConfigError("启用自定义指令前必须配置可执行的执行器")
        return value
    if executor_type == "http":
        return _validate_http_config(value)
    if executor_type == "builtin_function":
        name = value.get("name")
        if not isinstance(name, str) or name not in BUILTIN_FUNCTIONS:
            names = ", ".join(BUILTIN_FUNCTIONS)
            raise CustomCommandConfigError(f"内置函数必须是已注册函数：{names}")
        template = value.get("template", "")
        if not isinstance(template, str) or len(template) > 4096:
            raise CustomCommandConfigError("内置函数 template 不能超过 4096 个字符")
        return {"name": name, **({"template": template} if template else {})}
    if executor_type in {"python", "javascript"}:
        return _validate_script_config(executor_type, value, enabled=enabled)
    raise AssertionError("unreachable")


def _validate_http_config(config: dict[str, Any]) -> dict[str, Any]:
    url = config.get("url")
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise CustomCommandConfigError("HTTP 执行器需要有效的 url（最多 2048 个字符）")
    # Template variables are valid at runtime; use a harmless host for shape
    # validation while still rejecting credentials and unsupported schemes.
    candidate_url = _COMMAND_VARIABLE.sub("example.invalid", url.strip())
    parsed = urlsplit(candidate_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise CustomCommandConfigError("HTTP url 只支持带主机名的 http/https 地址")
    if parsed.username or parsed.password:
        raise CustomCommandConfigError("HTTP url 不允许携带用户名或密码")
    allow_http = config.get("allowInsecureHttp", False)
    if not isinstance(allow_http, bool):
        raise CustomCommandConfigError("allowInsecureHttp 必须是布尔值")
    if parsed.scheme == "http" and not allow_http:
        raise CustomCommandConfigError("HTTP 明文请求必须显式设置 allowInsecureHttp=true")
    method = config.get("method", "GET")
    if not isinstance(method, str) or method.upper() not in _HTTP_METHODS:
        raise CustomCommandConfigError(f"HTTP method 只支持：{', '.join(sorted(_HTTP_METHODS))}")
    timeout = config.get("timeoutSeconds", 10)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 1 <= timeout <= MAX_HTTP_TIMEOUT_SECONDS
    ):
        raise CustomCommandConfigError(f"timeoutSeconds 必须在 1-{MAX_HTTP_TIMEOUT_SECONDS} 秒之间")
    retries = config.get("retries", 0)
    if (
        not isinstance(retries, int)
        or isinstance(retries, bool)
        or not 0 <= retries <= MAX_HTTP_RETRIES
    ):
        raise CustomCommandConfigError(f"retries 必须在 0-{MAX_HTTP_RETRIES} 之间")
    headers = config.get("headers", {})
    if (
        not isinstance(headers, dict)
        or len(headers) > 50
        or any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in headers.items()
        )
    ):
        raise CustomCommandConfigError("headers 必须是最多 50 项的字符串对象")
    if any(key.casefold() == "host" for key in headers):
        raise CustomCommandConfigError("headers 不允许覆盖 Host")
    body = config.get("body")
    if body is not None:
        try:
            encoded_body = json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise CustomCommandConfigError("body 必须是合法 JSON 值") from exc
        if len(encoded_body.encode("utf-8")) > 32 * 1024:
            raise CustomCommandConfigError("body 不能超过 32KB")
    response_format = config.get("responseFormat", "text")
    if not isinstance(response_format, str) or response_format not in {"text", "json"}:
        raise CustomCommandConfigError("responseFormat 只能是 text 或 json")
    response_path = config.get("responsePath")
    if response_path is not None and (
        not isinstance(response_path, str)
        or not response_path
        or len(response_path) > 200
        or any(
            not part or not re.fullmatch(r"[A-Za-z0-9_-]+", part)
            for part in response_path.split(".")
        )
    ):
        raise CustomCommandConfigError("responsePath 必须是点分隔的 JSON 字段路径")
    max_response = config.get("maxResponseBytes", MAX_HTTP_RESPONSE_BYTES)
    if (
        not isinstance(max_response, int)
        or isinstance(max_response, bool)
        or not 1 <= max_response <= MAX_HTTP_RESPONSE_BYTES
    ):
        raise CustomCommandConfigError(f"maxResponseBytes 必须在 1-{MAX_HTTP_RESPONSE_BYTES} 之间")
    allowed_hosts = config.get("allowedHosts", [])
    if (
        not isinstance(allowed_hosts, list)
        or len(allowed_hosts) > 50
        or any(
            not isinstance(host, str) or not host.strip() or len(host) > 253
            for host in allowed_hosts
        )
    ):
        raise CustomCommandConfigError("allowedHosts 必须是最多 50 个主机名的数组")
    raw_parsed = urlsplit(url.strip())
    if _COMMAND_VARIABLE.search(raw_parsed.netloc or "") and not allowed_hosts:
        raise CustomCommandConfigError("url 主机名包含变量时必须显式配置 allowedHosts 白名单")
    follow_redirects = config.get("followRedirects", False)
    if follow_redirects is not False:
        raise CustomCommandConfigError("为避免绕过目标限制，followRedirects 必须为 false")
    return {
        "url": url.strip(),
        "method": method.upper(),
        "headers": headers,
        "body": body,
        "timeoutSeconds": timeout,
        "retries": retries,
        "responseFormat": response_format,
        **({"responsePath": response_path} if response_path is not None else {}),
        "maxResponseBytes": max_response,
        "allowedHosts": [host.strip().casefold() for host in allowed_hosts],
        **({"allowInsecureHttp": True} if allow_http else {}),
    }


def _validate_script_config(
    config_type: str, config: dict[str, Any], *, enabled: bool
) -> dict[str, Any]:
    code = config.get("code")
    if (
        not isinstance(code, str)
        or not code.strip()
        or len(code.encode("utf-8")) > MAX_SCRIPT_BYTES
    ):
        raise CustomCommandConfigError(f"{config_type} 执行器需要不超过 16KB 的 code")
    timeout = config.get("timeoutSeconds", 2)
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= MAX_SCRIPT_TIMEOUT_SECONDS
    ):
        raise CustomCommandConfigError(
            f"timeoutSeconds 必须在 1-{MAX_SCRIPT_TIMEOUT_SECONDS} 秒之间"
        )
    allow_execution = config.get("allowExecution", False)
    if not isinstance(allow_execution, bool):
        raise CustomCommandConfigError("allowExecution 必须是布尔值")
    if enabled and not allow_execution:
        raise CustomCommandConfigError("启用脚本执行器前必须显式设置 allowExecution=true")
    if config_type == "python":
        _validate_python_code(code)
    else:
        if "__" in code:
            raise CustomCommandConfigError("JavaScript code 不允许使用双下划线属性")
    return {"code": code, "timeoutSeconds": timeout, "allowExecution": allow_execution}


def _validate_python_code(code: str) -> None:
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise CustomCommandConfigError(f"Python code 语法错误：{exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise CustomCommandConfigError("Python code 不允许 import")
        if isinstance(node, ast.Name) and node.id in _SCRIPT_FORBIDDEN_NAMES:
            raise CustomCommandConfigError(f"Python code 不允许使用 {node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise CustomCommandConfigError("Python code 不允许访问下划线属性")
            if node.attr in _SCRIPT_FORBIDDEN_NAMES:
                raise CustomCommandConfigError(f"Python code 不允许访问 {node.attr}")


def _substitute(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                raise CustomCommandExecutorError(f"未定义的命令变量：{name}")
            return variables[name]

        return _COMMAND_VARIABLE.sub(replace, value)
    if isinstance(value, dict):
        return {key: _substitute(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, variables) for item in value]
    return value


async def execute_custom_command(
    executor_type: str, config: dict[str, Any], context: CustomCommandContext
) -> str:
    """Execute one validated custom command and return Telegram-safe text."""

    try:
        normalized = validate_executor_config(executor_type, config, enabled=True)
    except CustomCommandConfigError as exc:
        raise CustomCommandExecutorError(str(exc)) from exc
    if executor_type == "http":
        return await _execute_http(normalized, context)
    if executor_type == "builtin_function":
        return _execute_builtin(normalized, context)
    if executor_type in {"python", "javascript"}:
        return await _execute_script(executor_type, normalized, context)
    raise CustomCommandExecutorError("该自定义指令没有可执行的执行器")


def _execute_builtin(config: dict[str, Any], context: CustomCommandContext) -> str:
    name = config["name"]
    if name == "args":
        return context.argument
    if name == "echo":
        template = config.get("template", "{{args}}")
        return str(_substitute(template, context.variables()))
    if name == "json":
        return json.dumps(context.payload(), ensure_ascii=False, indent=2)
    if name == "utc_time":
        return datetime.now(UTC).isoformat()
    raise CustomCommandExecutorError("内置函数未注册")


async def _execute_http(config: dict[str, Any], context: CustomCommandContext) -> str:
    variables = context.variables()
    url = str(_substitute(config["url"], variables))
    parsed = urlsplit(url)
    pinned_addresses = await _assert_safe_target(parsed, config["allowedHosts"])
    headers = _substitute(config["headers"], variables)
    body = _substitute(config.get("body"), variables)
    timeout_seconds = float(config["timeoutSeconds"])
    retries = int(config["retries"])
    max_response_bytes = int(config["maxResponseBytes"])
    last_error: Exception | None = None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0)),
        follow_redirects=False,
        headers={"User-Agent": "tg-checkin-bot/custom-command"},
    ) as client:
        for attempt in range(retries + 1):
            try:
                if parsed.hostname and not _is_ip_literal(parsed.hostname):
                    current_addresses = await _resolve_public_addresses(parsed)
                    if current_addresses != pinned_addresses:
                        raise CustomCommandExecutorError(
                            "HTTP 目标主机解析结果发生变化，已拒绝请求"
                        )
                request_kwargs: dict[str, Any] = {"headers": headers}
                if body is not None:
                    if isinstance(body, (dict, list, int, float, bool)):
                        request_kwargs["json"] = body
                    else:
                        request_kwargs["content"] = str(body)
                async with client.stream(config["method"], url, **request_kwargs) as response:
                    if (
                        response.status_code in {408, 429} or response.status_code >= 500
                    ) and attempt < retries:
                        await asyncio.sleep(min(2**attempt, 2))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise CustomCommandExecutorError(
                            f"HTTP 执行器返回状态码 {response.status_code}"
                        )
                    chunks: list[bytes] = []
                    total_bytes = 0
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > max_response_bytes:
                            raise CustomCommandExecutorError("HTTP 响应超过配置的大小限制")
                        chunks.append(chunk)
                    return _format_http_response(b"".join(chunks), config)
            except CustomCommandExecutorError:
                raise
            except (httpx.HTTPError, TimeoutError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(min(2**attempt, 2))
                    continue
                break
    raise CustomCommandExecutorError("HTTP 执行器请求失败或超时") from last_error


def _format_http_response(raw_bytes: bytes, config: dict[str, Any]) -> str:
    if config["responseFormat"] == "json":
        try:
            value: Any = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CustomCommandExecutorError("HTTP 响应不是合法 JSON") from exc
        path = config.get("responsePath")
        if path:
            for part in path.split("."):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    raise CustomCommandExecutorError("HTTP 响应中不存在配置的 responsePath")
        result = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        )
    else:
        result = raw_bytes.decode("utf-8", errors="replace")
    return result[:MAX_HTTP_OUTPUT_CHARS]


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def _resolve_public_addresses(parsed: Any) -> set[str]:
    host = parsed.hostname
    if not isinstance(host, str) or not host:
        raise CustomCommandExecutorError("HTTP 目标地址无效")
    try:
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise CustomCommandExecutorError("HTTP 目标主机无法解析") from exc
    addresses = {item[4][0] for item in infos if item[4]}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise CustomCommandExecutorError("HTTP 目标地址解析到了内网或保留 IP")
    return addresses


async def _assert_safe_target(parsed: Any, allowed_hosts: list[str]) -> set[str]:
    if parsed.username or parsed.password:
        raise CustomCommandExecutorError("HTTP 目标地址不允许携带用户名或密码")
    host = parsed.hostname
    if not isinstance(host, str) or not host:
        raise CustomCommandExecutorError("HTTP 目标地址无效")
    host = host.rstrip(".").casefold()
    if allowed_hosts and host not in allowed_hosts:
        raise CustomCommandExecutorError("HTTP 目标主机不在 allowedHosts 中")
    if host in _PRIVATE_HOSTNAMES:
        raise CustomCommandExecutorError("HTTP 目标地址不允许访问本机或云元数据服务")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise CustomCommandExecutorError("HTTP 目标地址不允许访问内网或保留 IP")
        return {str(literal)}
    return await _resolve_public_addresses(parsed)


async def _execute_script(
    executor_type: str, config: dict[str, Any], context: CustomCommandContext
) -> str:
    if not config["allowExecution"]:
        raise CustomCommandExecutorError(
            f"{executor_type} 执行器默认禁用，请在配置中显式设置 allowExecution=true"
        )
    timeout_seconds = int(config["timeoutSeconds"])
    if executor_type == "python":
        command = [sys.executable, "-I", "-S", "-c", _python_runner(config["code"])]
        path = os.path.dirname(sys.executable)
    else:
        node = shutil.which("node")
        if node is None:
            raise CustomCommandExecutorError("当前环境未安装 Node.js，无法执行 JavaScript")
        command = [
            node,
            "--permission",
            "--allow-fs-read=/dev/stdin",
            "--allow-fs-write=/dev/stdout",
            "--no-addons",
            "--frozen-intrinsics",
            "-e",
            _javascript_runner(config["code"]),
        ]
        path = os.path.dirname(node)
    env = {"PATH": path, "PYTHONNOUSERSITE": "1", "LANG": "C.UTF-8"}
    with tempfile.TemporaryDirectory(prefix="tg-bot-command-") as directory:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=directory,
                env=env,
                start_new_session=True,
                preexec_fn=(
                    _resource_limits(timeout_seconds, memory_bytes=2 * 1024 * 1024 * 1024)
                    if executor_type == "javascript" and os.name == "posix"
                    else _resource_limits(timeout_seconds)
                    if os.name == "posix"
                    else None
                ),
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(
                    json.dumps(context.payload(), ensure_ascii=False).encode("utf-8")
                ),
                timeout=timeout_seconds + 1,
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise CustomCommandExecutorError("脚本执行超时") from exc
        except OSError as exc:
            raise CustomCommandExecutorError("脚本执行器启动失败") from exc
    if process.returncode != 0:
        raise CustomCommandExecutorError("脚本执行失败，请检查 code 和运行时限制")
    return stdout.decode("utf-8", errors="replace")[:MAX_SCRIPT_OUTPUT_CHARS].strip()


def _python_runner(code: str) -> str:
    # The user program only sees the JSON context and a bounded print helper;
    # dangerous builtins and internal modules are strictly restricted.
    return (
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "output = []\n"
        "def emit(*values, sep=' ', end='\\n'):\n"
        "    text = sep.join(str(value) for value in values) + end\n"
        "    remaining = 12288 - sum(len(item) for item in output)\n"
        "    if remaining > 0: output.append(text[:remaining])\n"
        "import builtins\n"
        "safe_builtins = {name: vars(builtins)[name] for name in "
        "('bool','dict','enumerate','float','int','len','list','max','min','range','round','sorted','str','sum','tuple')}\n"
        "safe_builtins['print'] = emit\n"
        "class _SafeJson:\n"
        "    __slots__ = ()\n"
        "    dumps = staticmethod(json.dumps)\n"
        "    loads = staticmethod(json.loads)\n"
        "safe_globals = {'__builtins__': safe_builtins, 'context': payload, 'json': _SafeJson, 'print': emit}\n"
        f"exec({code!r}, safe_globals, safe_globals)\n"
        "sys.stdout.write(''.join(output))\n"
    )


def _javascript_runner(code: str) -> str:
    # JavaScript runs directly in a permission-restricted child process. The
    # Node permission model blocks filesystem, network, child-process, addon,
    # and worker access; unlike node:vm it is an OS-enforced boundary.
    return (
        "const fs = require('fs');\n"
        "const context = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        "let output = '';\n"
        "const emit = (...values) => { if (output.length < 12288) output += values.map(String).join(' ') + '\\n'; };\n"
        "const console = Object.freeze({log: emit, info: emit});\n"
        f"{code}\n"
        "process.stdout.write(output.slice(0, 12288));\n"
    )


def _resource_limits(timeout_seconds: int, *, memory_bytes: int = 512 * 1024 * 1024):
    def limit() -> None:
        import resource

        cpu = timeout_seconds + 1
        for name, limits in (
            ("RLIMIT_CPU", (cpu, cpu)),
            ("RLIMIT_AS", (memory_bytes, memory_bytes)),
            ("RLIMIT_FSIZE", (128 * 1024, 128 * 1024)),
            # Node needs more than 32 descriptors during startup; this still
            # prevents descriptor exhaustion by user code.
            ("RLIMIT_NOFILE", (256, 256)),
        ):
            resource_name = getattr(resource, name, None)
            if resource_name is not None:
                with suppress(OSError, ValueError):
                    resource.setrlimit(resource_name, limits)

    return limit
