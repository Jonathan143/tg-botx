from __future__ import annotations

import base64
import math
import secrets
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, NoReturn

from tg_botx.core.time import utc_isoformat

ADMIN_KEY_MIN_BYTES = 32

DEFAULT_NONCE_TTL_SECONDS = 120

DEFAULT_TIMESTAMP_SKEW_SECONDS = 120


class SecurityError(Exception):
    """Safe, stable error that can be translated directly by the API layer."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after


def validate_admin_key(value: str | bytes) -> bytes:
    """Validate and return the exact UTF-8 bytes of an administrator key.

    Length by itself does not make a repeated placeholder a random secret, so
    obvious low-entropy and example values are rejected at startup as well.
    """

    if isinstance(value, str):
        key = value.encode("utf-8")
    elif isinstance(value, bytes):
        key = value
    else:
        raise SecurityError(
            "ADMIN_KEY_INVALID",
            "管理密钥配置无效，必须使用至少 32 个随机字节。",
            status_code=500,
        )

    normalized = key.strip().lower()
    example_values = {
        b"change-me",
        b"changeme",
        b"replace-me",
        b"your-admin-key",
        b"your_admin_key",
    }
    entropy = _shannon_entropy(key)
    if (
        len(key) < ADMIN_KEY_MIN_BYTES
        or not normalized
        or normalized in example_values
        or len(set(key)) < 8
        or entropy < 3.0
    ):
        raise SecurityError(
            "ADMIN_KEY_INVALID",
            "管理密钥配置无效，必须使用至少 32 个随机字节。",
            status_code=500,
        )
    return key


def _shannon_entropy(value: bytes) -> float:
    counts: dict[int, int] = defaultdict(int)
    for byte in value:
        counts[byte] += 1
    length = len(value)
    if not length:
        return 0.0
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _iso_timestamp(timestamp: float) -> str:
    return utc_isoformat(datetime.fromtimestamp(timestamp, tz=UTC)) or ""


def _timestamp_from_datetime(value: Any) -> float | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def _random_token(byte_count: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).rstrip(b"=").decode("ascii")


def _decode_base64(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 16_384:
        _authentication_failed()
    try:
        encoded = value.encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        return base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError):
        _authentication_failed()


def _authentication_failed() -> NoReturn:
    raise SecurityError("AUTH_FAILED", "认证失败。", status_code=401)


def _session_failed() -> NoReturn:
    raise SecurityError("SESSION_INVALID", "会话无效或已过期。", status_code=401)
