from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from tg_botx.features.bot.executors.schemas import origin, parsed_url, validate_headers


class HttpCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    origin: str
    headers: dict[str, SecretStr] = Field(repr=False)

    @model_validator(mode="after")
    def validate_credential(self) -> Self:
        target = parsed_url(self.origin)
        if target.scheme != "https" or target.path not in {"", "/"} or target.query:
            raise ValueError("凭据必须绑定无路径和查询参数的 HTTPS origin")
        validate_headers(
            {key: value.get_secret_value() for key, value in self.headers.items()}, credentials=True
        )
        return self


@dataclass(frozen=True, slots=True)
class ExecutorPolicy:
    allowed_origins: frozenset[str] = frozenset()
    credentials: dict[str, HttpCredential] = field(default_factory=dict, repr=False)
    python_enabled: bool = False
    max_workers: int = 8
    python_workers: int = 2
    queue_limit: int = 100
    queue_seconds: int = 60
    rate_limit: int = 5
    retention_days: int = 30

    def allows_url(self, value: str) -> bool:
        return origin(value) in self.allowed_origins


_BLOCKED_V6 = tuple(
    ipaddress.ip_network(item)
    for item in (
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2002::/16",
        "2001::/32",
    )
)


def public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if address == ipaddress.ip_address("168.63.129.16"):
        return False  # Azure platform/metadata virtual address, despite its public classification.
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return public_ip(str(address.ipv4_mapped))
        if any(address in network for network in _BLOCKED_V6):
            return False
    return True
