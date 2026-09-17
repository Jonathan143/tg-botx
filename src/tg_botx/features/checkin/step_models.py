"""工作流步骤字段契约；跨节点变量与数据源引用仍由语义校验器处理。"""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from tg_botx.features.message_library.models import (
    MAX_MESSAGES,
    MessageText,
    validate_message_text,
)


class StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: str | None = None


class SendMessageStep(StepBase):
    type: Literal["send_message"]
    text: str | None = None
    message_mode: Literal["fixed", "random"] = "fixed"
    random_source: Literal["manual", "group"] = "manual"
    messages: list[MessageText] | None = Field(default=None, max_length=MAX_MESSAGES)
    message_group_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def valid_source(self) -> SendMessageStep:
        if self.message_mode == "fixed":
            if self.text is None:
                raise ValueError("固定消息必须配置 text")
            validate_message_text(self.text)
            if (
                self.messages is not None
                or self.message_group_id is not None
                or self.random_source != "manual"
            ):
                raise ValueError("固定消息不能同时配置随机消息来源")
        else:
            if self.text is not None:
                raise ValueError("随机消息不能同时配置固定 text")
            if self.random_source == "manual":
                if self.message_group_id is not None or not self.messages:
                    raise ValueError("手动随机消息必须配置非空 messages，不能同时引用分组")
                for message in self.messages:
                    validate_message_text(message)
            else:
                if self.messages is not None or not self.message_group_id:
                    raise ValueError("实时分组必须配置 message_group_id，不能同时保存 messages")
                try:
                    UUID(self.message_group_id)
                except ValueError as exc:
                    raise ValueError("message_group_id 必须是有效分组 ID") from exc
        return self


class WaitMessageStep(StepBase):
    type: Literal["wait_message"]
    timeout_seconds: int = 60
    success: Any = None
    failure: Any = None


class ClickButtonStep(StepBase):
    type: Literal["click_button"]
    text: str | None = None
    text_contains: str | None = None
    callback_data: str | None = None
    row: int | None = None
    column: int | None = None


class HttpRequestStep(StepBase):
    type: Literal["http_request"]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str
    headers: str = ""
    body: str | None = None
    timeout_seconds: int = 30


class ExtractVariableStep(StepBase):
    type: Literal["extract_variable"]
    name: str
    source: Literal["http_body", "http_status", "http_headers", "wait_message_text"]
    source_node_id: str | None = None
    path: str | None = None
    value_type: Literal["text", "number", "datetime"] = "text"
    mode: Literal["whole_text", "first_number", "regex_capture", "metadata"] = "whole_text"
    pattern: str | None = None
    capture_group: int | str = 1
    field: str | None = None
    regex: dict[str, Any] | None = None
    extract_source: Literal["message_text", "metadata"] = "message_text"


class ConditionStep(StepBase):
    type: Literal["condition"]
    schema_version: int = 2
    strict: bool = False
    extracts: list[dict[str, Any]] = Field(default_factory=list)
    branches: list[dict[str, Any]]


Step = Annotated[
    SendMessageStep
    | WaitMessageStep
    | ClickButtonStep
    | HttpRequestStep
    | ExtractVariableStep
    | ConditionStep,
    Field(discriminator="type"),
]
STEP_ADAPTER: TypeAdapter[Step] = TypeAdapter(Step)
STEP_MODELS = (
    SendMessageStep,
    WaitMessageStep,
    ClickButtonStep,
    HttpRequestStep,
    ExtractVariableStep,
    ConditionStep,
)
STEP_FIELDS = {
    get_args(model.model_fields["type"].annotation)[0]: set(model.model_fields)
    for model in STEP_MODELS
}


def validate_step_shape(step: dict[str, Any]) -> None:
    """Validate canonical fields without changing omitted fields in stored YAML."""
    try:
        STEP_ADAPTER.validate_python(step)
    except ValidationError as exc:
        fields = [
            ".".join(str(part) for part in error["loc"])
            for error in exc.errors(include_input=False)
        ]
        raise ValueError("步骤字段格式无效：" + "、".join(fields)) from None
