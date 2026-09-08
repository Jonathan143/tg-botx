from __future__ import annotations

try:
    import regex as safe_regex
except ImportError:
    safe_regex = None

import time
from typing import Any

from tg_botx.features.checkin.conditions.types import (
    MAX_PATTERN_LENGTH,
    MAX_REGEX_INPUT_LENGTH,
    REGEX_MATCH_TIMEOUT_SECONDS,
    ConditionEvaluationError,
    RegexBudget,
)


def _regex_flags(config: dict[str, Any]) -> int:
    flags = 0
    if config.get("ignore_case", False):
        flags |= safe_regex.IGNORECASE
    if config.get("multiline", False):
        flags |= safe_regex.MULTILINE
    return flags


def compile_regex_pattern(pattern: str, config: dict[str, Any]) -> Any:
    """Compile a condition pattern using the same engine as runtime matching.

    Keeping compilation in one helper lets schema validation reject malformed
    patterns before a task is persisted, while runtime execution still retains
    its timeout and budget protections around the actual match operation.
    """
    if safe_regex is None:  # pragma: no cover - dependency is installed in production
        raise ConditionEvaluationError("条件正则依赖 regex 未安装")
    try:
        return safe_regex.compile(pattern, _regex_flags(config))
    except safe_regex.error as exc:
        raise ConditionEvaluationError(f"正则表达式无效：{exc}") from exc


def execute_regex(
    pattern: str,
    value: str,
    config: dict[str, Any],
    budget: RegexBudget,
    *,
    capture_group: int | str | None = None,
) -> str | bool:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ConditionEvaluationError(f"正则表达式不能超过 {MAX_PATTERN_LENGTH} 个字符")
    if len(value) > MAX_REGEX_INPUT_LENGTH:
        raise ConditionEvaluationError(f"正则输入不能超过 {MAX_REGEX_INPUT_LENGTH} 个字符")
    if budget.remaining <= 0:
        raise ConditionEvaluationError("条件节点正则执行预算已耗尽")
    if safe_regex is None:  # pragma: no cover - dependency is installed in production
        raise ConditionEvaluationError("条件正则依赖 regex 未安装")
    timeout = min(REGEX_MATCH_TIMEOUT_SECONDS, budget.remaining)
    started = time.perf_counter()
    try:
        compiled = compile_regex_pattern(pattern, config)
        if config.get("match_mode", "search") == "full":
            match = compiled.fullmatch(value, timeout=timeout)
        else:
            match = compiled.search(value, timeout=timeout)
    except TimeoutError as exc:
        raise ConditionEvaluationError("正则执行超时") from exc
    finally:
        budget.spend(time.perf_counter() - started)
    if capture_group is None:
        return match is not None
    if match is None:
        raise ConditionEvaluationError("正则未匹配")
    try:
        captured = match.group(capture_group)
    except (IndexError, KeyError) as exc:
        raise ConditionEvaluationError(f"正则捕获组不存在：{capture_group}") from exc
    if captured is None:
        raise ConditionEvaluationError(f"正则捕获组未参与匹配：{capture_group}")
    return str(captured)
