from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tg_botx.interfaces.admin.security.primitives import (
    DEFAULT_NONCE_TTL_SECONDS,
    DEFAULT_TIMESTAMP_SKEW_SECONDS,
    _authentication_failed,
    _decode_base64,
    _iso_timestamp,
    _random_token,
    validate_admin_key,
)


@dataclass(slots=True)
class _TransportKey:
    key_id: str
    private_key: rsa.RSAPrivateKey
    created_at: float
    retire_at: float | None = None


@dataclass(slots=True)
class _Nonce:
    key_id: str
    purpose: str
    expires_at: float


class TransportKeyManager:
    """In-memory RSA-OAEP transport keys and single-use nonce registry."""

    def __init__(
        self,
        *,
        rotation_hours: float = 24,
        old_key_grace_seconds: int = 300,
        nonce_ttl_seconds: int = DEFAULT_NONCE_TTL_SECONDS,
        key_size: int = 2048,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if rotation_hours <= 0:
            raise ValueError("rotation_hours must be positive")
        if old_key_grace_seconds < 0 or nonce_ttl_seconds <= 0:
            raise ValueError("key grace and nonce TTL must be valid")
        if key_size < 2048:
            raise ValueError("RSA key_size must be at least 2048")
        self.rotation_seconds = float(rotation_hours) * 3600
        self.old_key_grace_seconds = int(old_key_grace_seconds)
        self.nonce_ttl_seconds = int(nonce_ttl_seconds)
        self.key_size = key_size
        self._clock = clock
        self._lock = threading.RLock()
        self._old_keys: dict[str, _TransportKey] = {}
        self._nonces: dict[bytes, _Nonce] = {}
        self._current = self._generate_key(self._clock())

    @property
    def current_key_id(self) -> str:
        with self._lock:
            self._rotate_if_due_locked(self._clock())
            return self._current.key_id

    def issue_challenge(self, purpose: str) -> dict[str, str]:
        """Return a browser-importable SPKI PEM key and a bound nonce."""

        purpose = _validate_purpose(purpose)
        with self._lock:
            now = self._clock()
            self._rotate_if_due_locked(now)
            self._prune_locked(now)
            nonce = _random_token()
            expires_at = now + self.nonce_ttl_seconds
            self._nonces[_nonce_digest(nonce)] = _Nonce(
                key_id=self._current.key_id,
                purpose=purpose,
                expires_at=expires_at,
            )
            public_pem = self._current.private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            return {
                "keyId": self._current.key_id,
                "publicKey": public_pem.decode("ascii"),
                "nonce": nonce,
                "expiresAt": _iso_timestamp(expires_at),
                "algorithm": "RSA-OAEP-256",
            }

    def decrypt_payload(
        self,
        key_id: str,
        ciphertext_b64: str,
        purpose: str,
        *,
        max_timestamp_skew_seconds: int | None = DEFAULT_TIMESTAMP_SKEW_SECONDS,
    ) -> dict[str, Any]:
        """Decrypt JSON and atomically consume its nonce.

        All malformed ciphertext, stale timestamps and nonce errors intentionally
        have the same outward-facing error.
        """

        purpose = _validate_purpose(purpose)
        with self._lock:
            now = self._clock()
            self._rotate_if_due_locked(now)
            self._prune_locked(now)
            transport_key = self._find_key_locked(key_id, now)
            if transport_key is None:
                _authentication_failed()

            ciphertext = _decode_base64(ciphertext_b64)
            expected_ciphertext_size = (transport_key.private_key.key_size + 7) // 8
            if len(ciphertext) != expected_ciphertext_size:
                _authentication_failed()
            try:
                plaintext = transport_key.private_key.decrypt(
                    ciphertext,
                    padding.OAEP(
                        mgf=padding.MGF1(algorithm=hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=None,
                    ),
                )
                payload = json.loads(plaintext.decode("utf-8"), object_pairs_hook=_unique_object)
            except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                _authentication_failed()
            if not isinstance(payload, dict):
                _authentication_failed()

            nonce = payload.get("nonce")
            if not isinstance(nonce, str):
                _authentication_failed()
            nonce_record = self._nonces.pop(_nonce_digest(nonce), None)
            if (
                nonce_record is None
                or nonce_record.expires_at <= now
                or not hmac.compare_digest(nonce_record.key_id, key_id)
                or not hmac.compare_digest(nonce_record.purpose, purpose)
            ):
                _authentication_failed()

            # Every browser-encrypted secret uses one canonical plaintext
            # shape.  This check intentionally follows nonce consumption so
            # even a malformed attempt cannot reuse a one-time challenge.
            if set(payload) != {"value", "nonce", "timestamp"}:
                _authentication_failed()
            if not isinstance(payload.get("value"), str) or not isinstance(
                payload.get("timestamp"), str
            ):
                _authentication_failed()
            if max_timestamp_skew_seconds is not None:
                timestamp = _parse_browser_timestamp(payload.get("timestamp"))
                if timestamp is None or abs(now - timestamp) > max_timestamp_skew_seconds:
                    _authentication_failed()
            return payload

    def verify_admin_payload(
        self,
        key_id: str,
        ciphertext_b64: str,
        admin_key: str | bytes,
        *,
        purpose: str = "admin",
        max_timestamp_skew_seconds: int = DEFAULT_TIMESTAMP_SKEW_SECONDS,
    ) -> dict[str, Any]:
        """Decrypt and verify exactly ``{value, nonce, timestamp}``."""

        configured_key = validate_admin_key(admin_key)
        payload = self.decrypt_payload(
            key_id,
            ciphertext_b64,
            purpose,
            max_timestamp_skew_seconds=max_timestamp_skew_seconds,
        )
        supplied_key = payload["value"]
        supplied_bytes = supplied_key.encode("utf-8")
        # Hashing equalizes compare lengths while compare_digest avoids a
        # content-dependent early exit.
        if not hmac.compare_digest(
            hashlib.sha256(supplied_bytes).digest(),
            hashlib.sha256(configured_key).digest(),
        ):
            _authentication_failed()
        return payload

    def rotate_now(self) -> str:
        """Rotate immediately and retain the former private key for its grace."""

        with self._lock:
            now = self._clock()
            previous = self._current
            previous.retire_at = now + self.old_key_grace_seconds
            self._old_keys[previous.key_id] = previous
            self._current = self._generate_key(now)
            self._prune_locked(now)
            return self._current.key_id

    def rotate_if_due(self) -> bool:
        with self._lock:
            now = self._clock()
            old_id = self._current.key_id
            self._rotate_if_due_locked(now)
            self._prune_locked(now)
            return old_id != self._current.key_id

    def prune(self) -> None:
        with self._lock:
            self._prune_locked(self._clock())

    async def rotation_loop(self, stop_event: asyncio.Event | None = None) -> None:
        """Periodically rotate keys; cancellation is the normal shutdown path."""

        while stop_event is None or not stop_event.is_set():
            self.rotate_if_due()
            delay = min(60.0, max(0.1, self.rotation_seconds))
            if stop_event is None:
                await asyncio.sleep(delay)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=delay)

    def _generate_key(self, now: float) -> _TransportKey:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=self.key_size)
        public_der = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_id = (
            base64.urlsafe_b64encode(hashlib.sha256(public_der).digest()[:18]).rstrip(b"=").decode()
        )
        return _TransportKey(key_id=key_id, private_key=private_key, created_at=now)

    def _rotate_if_due_locked(self, now: float) -> None:
        if now - self._current.created_at < self.rotation_seconds:
            return
        previous = self._current
        previous.retire_at = now + self.old_key_grace_seconds
        self._old_keys[previous.key_id] = previous
        self._current = self._generate_key(now)

    def _find_key_locked(self, key_id: str, now: float) -> _TransportKey | None:
        if not isinstance(key_id, str):
            return None
        if hmac.compare_digest(self._current.key_id, key_id):
            return self._current
        key = self._old_keys.get(key_id)
        if key is not None and key.retire_at is not None and key.retire_at > now:
            return key
        return None

    def _prune_locked(self, now: float) -> None:
        self._old_keys = {
            key_id: key
            for key_id, key in self._old_keys.items()
            if key.retire_at is not None and key.retire_at > now
        }
        self._nonces = {
            nonce_hash: nonce
            for nonce_hash, nonce in self._nonces.items()
            if nonce.expires_at > now
            and (nonce.key_id == self._current.key_id or nonce.key_id in self._old_keys)
        }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_purpose(purpose: str) -> str:
    if not isinstance(purpose, str) or not purpose or len(purpose) > 64:
        raise ValueError("purpose must be a non-empty string of at most 64 characters")
    return purpose


def _nonce_digest(nonce: str) -> bytes:
    return hashlib.sha256(nonce.encode("utf-8", errors="replace")).digest()


def _parse_browser_timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str):
        try:
            timestamp = float(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    return None
                timestamp = parsed.timestamp()
            except (ValueError, OverflowError):
                return None
    else:
        return None
    # Date.now() uses milliseconds, while many clients use Unix seconds.
    if timestamp > 100_000_000_000:
        timestamp /= 1000
    if not math.isfinite(timestamp) or timestamp < 0:
        return None
    return timestamp
