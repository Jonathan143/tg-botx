from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tg_botx.features.checkin.validation import (
    _validate_extraction_sources,
    _validate_step_sequence,
)

_MODEL_CONFIG = ConfigDict(extra="forbid")


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
    frequency: Literal["daily", "every_n_days", "weekly", "monthly_dates"] = "daily"
    start_date: date | None = None
    end_date: date | None = None
    interval_days: int | None = Field(default=None, ge=1, le=365)
    weekdays: list[int] | None = None
    month_days: list[int] | None = None
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
    def validate_shape(self) -> ScheduleConfig:
        if self.type == "fixed" and not self.time:
            raise ValueError("fixed 调度必须配置 time")
        if self.type == "random" and (not self.start or not self.end):
            raise ValueError("random 调度必须配置 start 和 end")
        if self.type == "random":
            assert self.start is not None and self.end is not None
            if time.fromisoformat(self.end) <= time.fromisoformat(self.start):
                raise ValueError("随机时间窗口暂不支持跨午夜，end 必须晚于 start")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("schedule.end_date 不能早于 start_date")
        if self.frequency == "every_n_days":
            if self.interval_days is None:
                raise ValueError("every_n_days 调度必须配置 interval_days")
        elif self.interval_days is not None:
            raise ValueError("interval_days 只能用于 every_n_days 调度")
        if self.frequency == "weekly":
            if not self.weekdays:
                raise ValueError("weekly 调度至少选择一天")
            if any(isinstance(day, bool) or day < 1 or day > 7 for day in self.weekdays):
                raise ValueError("weekdays 必须是 1-7 的 ISO 星期编号")
            if len(set(self.weekdays)) != len(self.weekdays):
                raise ValueError("weekdays 不能包含重复值")
        elif self.weekdays is not None:
            raise ValueError("weekdays 只能用于 weekly 调度")
        if self.frequency == "monthly_dates":
            if not self.month_days:
                raise ValueError("monthly_dates 调度至少选择一个日期")
            if any(isinstance(day, bool) or day < 1 or day > 31 for day in self.month_days):
                raise ValueError("month_days 必须是 1-31 的日期")
            if len(set(self.month_days)) != len(self.month_days):
                raise ValueError("month_days 不能包含重复值")
        elif self.month_days is not None:
            raise ValueError("month_days 只能用于 monthly_dates 调度")
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
    log_bot_response: bool | None = None
    log_condition_values: bool | None = None
    notify_bot_response: bool | None = None

    @model_validator(mode="after")
    def validate_steps(self) -> TaskDefinition:
        _validate_step_sequence(
            self.steps,
            "steps",
            condition_depth=0,
            definite={},
            possible={},
            has_wait=False,
            node_ids=set(),
        )
        _validate_extraction_sources(self.steps, "steps", {}, set())
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> TaskDefinition:
        with path.open("r", encoding="utf-8") as file:
            return cls.model_validate(yaml.safe_load(file))

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False
        )

    def to_api_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
