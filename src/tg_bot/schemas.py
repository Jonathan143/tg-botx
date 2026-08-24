from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: list[int] = Field(default_factory=lambda: [30, 60, 120])


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

    @model_validator(mode="after")
    def validate_shape(self) -> "ScheduleConfig":
        if self.type == "fixed" and not self.time:
            raise ValueError("fixed 调度必须配置 time")
        if self.type == "random" and (not self.start or not self.end):
            raise ValueError("random 调度必须配置 start 和 end")
        if self.type == "random" and time.fromisoformat(self.end) <= time.fromisoformat(self.start):
            raise ValueError("随机时间窗口暂不支持跨午夜，end 必须晚于 start")
        return self


class TaskDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=150)
    account: str = "default"
    target: str = Field(min_length=1, max_length=200)
    schedule: ScheduleConfig
    retry: RetryConfig = Field(default_factory=RetryConfig)
    steps: list[dict[str, Any]] = Field(min_length=1)
    notifications: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_steps(self) -> "TaskDefinition":
        valid_types = {"send_message", "wait_message", "click_button"}
        for index, step in enumerate(self.steps):
            if step.get("type") not in valid_types:
                raise ValueError(f"steps[{index}] 的 type 必须是: {', '.join(sorted(valid_types))}")
            if step["type"] == "send_message" and not isinstance(step.get("text"), str):
                raise ValueError(f"steps[{index}] send_message 必须配置 text")
            if step["type"] == "click_button" and not any(
                key in step for key in ("text", "callback_data", "row", "column")
            ):
                raise ValueError(f"steps[{index}] click_button 至少需要一种按钮定位方式")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> "TaskDefinition":
        with path.open("r", encoding="utf-8") as file:
            return cls.model_validate(yaml.safe_load(file))

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
