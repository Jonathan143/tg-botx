from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from tg_botx.features.bot.executors.base import CommandExecutor, ExecutionError, json_bytes
from tg_botx.features.bot.executors.builtin import validate_builtin
from tg_botx.features.bot.executors.policy import ExecutorPolicy, public_ip
from tg_botx.features.bot.executors.schemas import (
    CONFIG_MODELS,
    BuiltinConfig,
    HttpConfig,
    code_hash,
    origin,
    parsed_url,
)


@dataclass(frozen=True, slots=True)
class ExecutionStatus:
    state: str
    reason: str | None = None
    code: str | None = None


class ExecutorRegistry:
    def __init__(self, policy: ExecutorPolicy | None = None):
        self.policy = policy if policy is not None else ExecutorPolicy()
        self.executors: dict[str, CommandExecutor] = {}
        self.python_available = False

    def normalize(
        self, kind: str, config: dict[str, Any], *, draft: bool = False
    ) -> dict[str, Any]:
        json_bytes(config)
        if kind == "none":
            if config:
                raise ValueError("none 不接受执行器配置")
            return {}
        if kind not in CONFIG_MODELS:
            raise ValueError("不支持的指令执行器")
        try:
            parsed = CONFIG_MODELS[kind].model_validate(config)
            if isinstance(parsed, BuiltinConfig):
                validate_builtin(parsed)
        except ValidationError as exc:
            if draft and all(error["type"] == "missing" for error in exc.errors()):
                return config  # Missing required fields are an explicitly disabled draft.
            # Never echo input values (including source code, headers, or tokens).
            fields = ", ".join(
                ".".join(map(str, error["loc"])) or "executorConfig" for error in exc.errors()
            )
            raise ValueError(f"执行器配置字段无效：{fields}") from exc
        return parsed.model_dump(by_alias=True, mode="json", exclude_none=True)

    def status(
        self, kind: str, config: dict[str, Any], confirmation: str | None = None
    ) -> ExecutionStatus:
        if kind == "javascript":
            return ExecutionStatus(
                "retired", "JavaScript 执行器已移除，请更换执行器", "EXECUTOR_RETIRED"
            )
        if kind == "none":
            return ExecutionStatus("not_configured", "尚未配置执行器", "EXECUTOR_NOT_CONFIGURED")
        if kind not in CONFIG_MODELS:
            return ExecutionStatus(
                "unsupported", "无法识别的历史执行器类型", "INVALID_EXECUTOR_CONFIG"
            )
        try:
            normalized = self.normalize(kind, config)
        except (ValueError, ExecutionError):
            return ExecutionStatus(
                "invalid_config", "执行器配置未通过校验", "INVALID_EXECUTOR_CONFIG"
            )
        if kind == "http":
            parsed = HttpConfig.model_validate(normalized)
            host = parsed_url(parsed.url).host
            try:
                ipaddress.ip_address(host)
                literal_blocked = not public_ip(host)
            except ValueError:
                literal_blocked = False
            if literal_blocked or not self.policy.allows_url(parsed.url):
                return ExecutionStatus(
                    "blocked_by_policy", "目标 origin 未加入部署端白名单", "HTTP_TARGET_BLOCKED"
                )
            if parsed.credential_ref:
                credential = self.policy.credentials.get(parsed.credential_ref)
                if credential is None or origin(credential.origin) != origin(parsed.url):
                    return ExecutionStatus(
                        "blocked_by_policy", "凭据不存在或未绑定该目标", "HTTP_TARGET_BLOCKED"
                    )
        if kind == "python":
            if not self.policy.python_enabled:
                return ExecutionStatus(
                    "blocked_by_policy", "部署端未开启 Python 执行", "EXECUTOR_UNAVAILABLE"
                )
            if confirmation != code_hash(config):
                return ExecutionStatus(
                    "invalid_config", "Python 源码尚未确认启用", "INVALID_EXECUTOR_CONFIG"
                )
            if not self.python_available:
                return ExecutionStatus(
                    "unavailable", "Python Runner 当前不可用", "EXECUTOR_UNAVAILABLE"
                )
        return ExecutionStatus("ready")

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "type": kind,
                "available": kind == "builtin_function"
                or (
                    bool(self.policy.allowed_origins)
                    if kind == "http"
                    else self.policy.python_enabled and self.python_available
                ),
                "configSchema": model.model_json_schema(by_alias=True),
                "requiresCodeConfirmation": kind == "python",
            }
            for kind, model in CONFIG_MODELS.items()
        ]

    async def close(self) -> None:
        import asyncio

        results = await asyncio.gather(
            *(item.close() for item in self.executors.values()), return_exceptions=True
        )
        if any(isinstance(item, BaseException) for item in results):
            raise ExecutionError("EXECUTION_FAILED")
