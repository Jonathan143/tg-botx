from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_MESSAGES = 1000
MAX_MESSAGE_LENGTH = 4096
MessageText = Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)]


class MessageGroupNotFound(LookupError):
    pass


class MessageGroupConflict(ValueError):
    pass


def validate_message_text(value: str) -> str:
    if not value.strip():
        raise ValueError("消息内容不能为空或仅包含空白字符")
    try:
        length = len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValueError("消息包含无效的 Unicode 字符") from exc
    if length > MAX_MESSAGE_LENGTH:
        raise ValueError("单条消息不能超过 4096 个 UTF-16 字符单位")
    return value


class MessageGroupWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=100)
    messages: list[MessageText] = Field(default_factory=list, max_length=MAX_MESSAGES)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("分组名称不能为空")
        return value

    @field_validator("messages")
    @classmethod
    def valid_messages(cls, values: list[str]) -> list[str]:
        for value in values:
            validate_message_text(value)
        return values


class MessageGroupUpdate(MessageGroupWrite):
    revision: int = Field(ge=1, strict=True)


class MessageGroupDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: int = Field(ge=1, strict=True)


class MessageGroupSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    message_count: int = Field(serialization_alias="messageCount")
    revision: int
    created_at: datetime = Field(serialization_alias="createdAt")
    updated_at: datetime = Field(serialization_alias="updatedAt")


class MessageGroupDetail(MessageGroupSummary):
    messages: list[MessageText] = Field(max_length=MAX_MESSAGES)

    @field_validator("messages")
    @classmethod
    def valid_messages(cls, values: list[str]) -> list[str]:
        for value in values:
            validate_message_text(value)
        return values
