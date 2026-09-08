from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tg_botx.features.bot.models import CommandRole, ExecutorType
from tg_botx.schemas import TaskDefinition

logger = logging.getLogger(__name__)


class AdminVerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str = Field(alias="keyId")
    ciphertext: str


class TaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: TaskDefinition


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    yaml: str = Field(min_length=1, max_length=1_000_000)
    overwrite_names: list[str] = Field(default_factory=list, alias="overwriteNames")


class LoginStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_name: str = Field(alias="accountName", min_length=1, max_length=100)
    method: Literal["qr", "phone"]


class EncryptedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str = Field(alias="keyId")
    ciphertext: str


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BotBindingBatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user"] = "user"
    quantity: int = Field(default=1, ge=1, le=100)
    ttl_days: int | None = Field(default=1, alias="ttlDays")


class BotAdminBindingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_days: int | None = Field(default=1, alias="ttlDays")


class BotCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, max_length=256)
    command: str | None = Field(default=None, min_length=1, max_length=32)
    enabled: bool | None = None
    menu_visible: bool | None = Field(default=None, alias="menuVisible")
    allowed_roles: list[CommandRole] | None = Field(
        default=None, alias="allowedRoles", max_length=3
    )


class BotCommandCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=256)
    enabled: bool = False
    menu_visible: bool = Field(default=False, alias="menuVisible")
    allowed_roles: list[CommandRole] = Field(
        default_factory=list, alias="allowedRoles", max_length=3
    )
    executor_type: ExecutorType = Field(default="none", alias="executorType")
    executor_config: dict[str, Any] = Field(default_factory=dict, alias="executorConfig")


class BotCommandOrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[str] = Field(min_length=1, max_length=500)


class BotCheckinConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_points: int = Field(alias="minPoints", ge=1, le=1_000_000)
    max_points: int = Field(alias="maxPoints", ge=1, le=1_000_000)


class MessageProbeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=4096)
    timeout_seconds: int = Field(default=30, alias="timeoutSeconds", ge=1, le=120)


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release_note: str | None = Field(default=None, alias="releaseNote", max_length=500)
