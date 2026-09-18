from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Protocol

MAX_CONFIG_BYTES = 32 * 1024
MAX_RESULT_BYTES = 16 * 1024
MAX_REPLY_UNITS = 3500

ERROR_MESSAGES = {
    "IDEMPOTENCY_CONFLICT": "同一幂等键不能用于不同的试运行请求。",
    "INVALID_EXECUTOR_CONFIG": "执行器配置无效，请联系管理员。",
    "EXECUTOR_RETIRED": "该执行器已移除，请联系管理员更换。",
    "EXECUTOR_UNAVAILABLE": "执行器暂时不可用，请稍后重试。",
    "EXECUTOR_NOT_CONFIGURED": "该命令尚未配置执行器。",
    "EXECUTION_FORBIDDEN": "你没有权限执行该命令。",
    "CONFIG_CHANGED": "命令配置已变更，本次执行已取消，请重新发送命令。",
    "HTTP_TARGET_BLOCKED": "请求目标不符合网络安全策略。",
    "HTTP_REQUEST_FAILED": "外部服务请求失败。",
    "HTTP_RESPONSE_INVALID": "外部服务返回了无法处理的响应。",
    "EXECUTION_TIMEOUT": "命令执行超时。外部操作可能已生效，请勿盲目重试。",
    "OUTPUT_TOO_LARGE": "执行结果超过大小限制。",
    "EXECUTION_FAILED": "命令执行失败，请联系管理员。",
    "EXECUTION_BUSY": "当前命令执行繁忙，请稍后重试。",
    "EXECUTION_RATE_LIMITED": "调用过于频繁，请稍后重试。",
    "QUEUE_EXPIRED": "命令排队已超时，请重新发送命令。",
    "EXECUTION_INTERRUPTED": "执行被中断，结果无法确认。请先核实外部操作状态。",
    "SERVICE_STOPPING": "服务正在停止，请稍后重试。",
    "MISSING_TEMPLATE_VARIABLE": "该执行上下文缺少模板所需的变量。",
}


class ExecutionError(RuntimeError):
    """Only stable, non-sensitive codes cross executor boundaries."""

    def __init__(self, code: str):
        self.code = code if code in ERROR_MESSAGES else "EXECUTION_FAILED"
        super().__init__(ERROR_MESSAGES[self.code])


def json_bytes(value: Any, *, maximum: int = MAX_CONFIG_BYTES) -> bytes:
    """Bound JSON structure, reject non-finite numbers and invalid Unicode."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited > 4096 or depth > 16:
            raise ExecutionError("OUTPUT_TOO_LARGE")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ExecutionError("INVALID_EXECUTOR_CONFIG")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ExecutionError("INVALID_EXECUTOR_CONFIG")
        elif item is not None and type(item) not in {str, bool, int}:
            raise ExecutionError("INVALID_EXECUTOR_CONFIG")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ExecutionError("INVALID_EXECUTOR_CONFIG") from exc
    if len(encoded) > maximum:
        raise ExecutionError("OUTPUT_TOO_LARGE")
    return encoded


@dataclass(frozen=True, slots=True)
class CommandContext:
    execution_id: str
    command: str
    argument: str
    user_id: int | None
    chat_id: int | None
    role: str

    def payload(self) -> dict[str, Any]:
        return {
            "executionId": self.execution_id,
            "command": self.command,
            "argument": self.argument,
            "user": {"id": self.user_id, "role": self.role},
            "chat": {"id": self.chat_id},
        }


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    text: str
    data: dict[str, Any] | None = None

    def validated(self) -> ExecutionResult:
        try:
            units = len(self.text.encode("utf-16-le")) // 2
        except UnicodeError as exc:
            raise ExecutionError("HTTP_RESPONSE_INVALID") from exc
        if not self.text or units > MAX_REPLY_UNITS:
            raise ExecutionError("OUTPUT_TOO_LARGE" if self.text else "HTTP_RESPONSE_INVALID")
        json_bytes(self.payload(), maximum=MAX_RESULT_BYTES)
        return self

    def payload(self) -> dict[str, Any]:
        return {"text": self.text, "data": self.data}

    @classmethod
    def from_payload(cls, value: Any) -> ExecutionResult:
        if (
            not isinstance(value, dict)
            or set(value) - {"text", "data"}
            or not isinstance(value.get("text"), str)
            or (value.get("data") is not None and not isinstance(value["data"], dict))
        ):
            raise ExecutionError("HTTP_RESPONSE_INVALID")
        return cls(value["text"], value.get("data")).validated()


class CommandExecutor(Protocol):
    async def execute(self, config: dict[str, Any], context: CommandContext) -> ExecutionResult: ...

    async def close(self) -> None: ...
