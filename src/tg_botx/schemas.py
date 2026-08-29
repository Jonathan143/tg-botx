from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


_MODEL_CONFIG = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)
_STEP_INPUT_ALIASES = {
    "timeoutSeconds": "timeout_seconds",
    "textContains": "text_contains",
    "callbackData": "callback_data",
}
_STEP_OUTPUT_ALIASES = {value: key for key, value in _STEP_INPUT_ALIASES.items()}


class RetryConfig(BaseModel):
    model_config = _MODEL_CONFIG

    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=lambda: [30, 60, 120]
    )


class NotificationConfig(BaseModel):
    model_config = _MODEL_CONFIG

    failure: bool = True
    success: bool = False


class ScheduleConfig(BaseModel):
    model_config = _MODEL_CONFIG

    type: Literal["fixed", "random"]
    timezone: str = "Asia/Shanghai"
    time: str | None = None
    start: str | None = None
    end: str | None = None

    @field_validator("time", "start", "end")
    @classmethod
    def valid_time(cls, value: str | None) -> str | None:
        if value is not None:
            time.fromisoformat(value)
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("调度 timezone 无效") from exc
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> "ScheduleConfig":
        if self.type == "fixed" and not self.time:
            raise ValueError("fixed 调度必须配置 time")
        if self.type == "random" and (not self.start or not self.end):
            raise ValueError("random 调度必须配置 start 和 end")
        if self.type == "random":
            assert self.start is not None and self.end is not None
            if time.fromisoformat(self.end) <= time.fromisoformat(self.start):
                raise ValueError("随机时间窗口暂不支持跨午夜，end 必须晚于 start")
        return self


class TaskDefinition(BaseModel):
    model_config = _MODEL_CONFIG

    name: str = Field(min_length=1, max_length=150)
    account: str = "default"
    target: str = Field(min_length=1, max_length=200)
    schedule: ScheduleConfig
    retry: RetryConfig = Field(default_factory=RetryConfig)
    steps: list[dict[str, Any]] = Field(min_length=1)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    output_bot_response: bool = False
    log_bot_response: bool | None = None
    notify_bot_response: bool | None = None

    @field_validator("steps", mode="before")
    @classmethod
    def normalize_step_aliases(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized = []
        for step in value:
            if not isinstance(step, dict):
                normalized.append(step)
                continue
            normalized.append(
                {_STEP_INPUT_ALIASES.get(key, key): item for key, item in step.items()}
            )
        return normalized

    @model_validator(mode="after")
    def validate_steps(self) -> "TaskDefinition":
        valid_types = {"send_message", "wait_message", "click_button"}
        for index, step in enumerate(self.steps):
            if step.get("type") not in valid_types:
                raise ValueError(f"steps[{index}] 的 type 必须是: {', '.join(sorted(valid_types))}")
            if step["type"] == "send_message" and not isinstance(step.get("text"), str):
                raise ValueError(f"steps[{index}] send_message 必须配置 text")
            if step["type"] == "click_button" and not any(
                key in step for key in ("text", "text_contains", "callback_data", "row", "column")
            ):
                raise ValueError(f"steps[{index}] click_button 至少需要一种按钮定位方式")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> "TaskDefinition":
        with path.open("r", encoding="utf-8") as file:
            return cls.model_validate(yaml.safe_load(file))

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False
        )

    def to_api_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", by_alias=True)
        payload["steps"] = [
            {_STEP_OUTPUT_ALIASES.get(key, key): value for key, value in step.items()}
            for step in payload["steps"]
        ]
        return payload
