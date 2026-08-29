"""Security primitives used by the HTTP administration API.

The module deliberately has no FastAPI dependency. Transport keys and nonces
remain process-local, while administrator session metadata may be persisted by
the caller so a service restart does not force an otherwise valid browser
session to re-enter the administrator key.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

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
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def _iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def _timestamp_from_datetime(value: Any) -> float | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def _random_token(byte_count: int = 32) -> str:
    return (
        base64.urlsafe_b64encode(secrets.token_bytes(byte_count))
        .rstrip(b"=")
        .decode("ascii")
    )


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
                payload = json.loads(
                    plaintext.decode("utf-8"), object_pairs_hook=_unique_object
                )
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
                if (
                    timestamp is None
                    or abs(now - timestamp) > max_timestamp_skew_seconds
                ):
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
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _generate_key(self, now: float) -> _TransportKey:
        private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=self.key_size
        )
        public_der = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_id = (
            base64.urlsafe_b64encode(hashlib.sha256(public_der).digest()[:18])
            .rstrip(b"=")
            .decode()
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


@dataclass(frozen=True, slots=True)
class SessionCredentials:
    token: str
    csrf_token: str
    expires_at: datetime
    renewed: bool = False


@dataclass(slots=True)
class _Session:
    csrf_token: str
    csrf_digest: bytes
    expires_at: float
    last_seen_at: float


class SessionStore(Protocol):
    """Minimal persistence contract used by :class:`SessionManager`."""

    def save_admin_session(self, token_hash: str, expires_at: datetime, last_seen_at: datetime) -> None: ...

    def get_admin_session(self, token_hash: str) -> Any | None: ...

    def delete_admin_session(self, token_hash: str) -> None: ...

    def delete_expired_admin_sessions(self, now: datetime) -> None: ...

    def delete_all_admin_sessions(self) -> None: ...


class SessionManager:
    """Opaque, random administrator sessions with a rolling TTL.

    When ``session_store`` is supplied, only token digests and timestamps are
    persisted. The digest key is derived from the administrator key, allowing
    validation after a process restart while making sessions invalid whenever
    the configured administrator key changes.
    """

    def __init__(
        self,
        admin_key: str | bytes,
        *,
        session_days: float = 30,
        clock: Callable[[], float] = time.time,
        session_store: SessionStore | None = None,
        prune_interval_seconds: float = 60,
    ) -> None:
        if session_days <= 0:
            raise ValueError("session_days must be positive")
        if prune_interval_seconds <= 0:
            raise ValueError("prune_interval_seconds must be positive")
        key = validate_admin_key(admin_key)
        self.session_seconds = float(session_days) * 86_400
        self.prune_interval_seconds = float(prune_interval_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._token_hash_key = hmac.new(
            key, b"tg-bot-admin-session-token-v1", hashlib.sha256
        ).digest()
        self._csrf_key = hmac.new(
            key, b"tg-bot-admin-session-csrf-v1", hashlib.sha256
        ).digest()
        self._session_store = session_store
        self._sessions: dict[bytes, _Session] = {}
        self._last_prune_at: float | None = None

    def create(self) -> SessionCredentials:
        now = self._clock()
        token = _random_token()
        csrf_token = self._csrf_token(token)
        expires_at = now + self.session_seconds
        with self._lock:
            self._prune_locked(now)
            token_hash = self._token_digest(token)
            session = _Session(
                csrf_token=csrf_token,
                csrf_digest=hashlib.sha256(csrf_token.encode("ascii")).digest(),
                expires_at=expires_at,
                last_seen_at=now,
            )
            self._sessions[token_hash] = session
            if self._session_store is not None:
                self._session_store.save_admin_session(
                    token_hash.hex(),
                    datetime.fromtimestamp(expires_at, tz=UTC),
                    datetime.fromtimestamp(now, tz=UTC),
                )
        return SessionCredentials(
            token,
            csrf_token,
            datetime.fromtimestamp(expires_at, tz=UTC),
        )

    def authenticate(
        self,
        token: str | None,
        csrf_token: str | None = None,
        *,
        require_csrf: bool = False,
    ) -> SessionCredentials:
        if not isinstance(token, str) or not token:
            _session_failed()
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            token_hash = self._token_digest(token)
            session = self._sessions.get(token_hash)
            if session is None and self._session_store is not None:
                persisted = self._session_store.get_admin_session(token_hash.hex())
                if persisted is not None:
                    expires_at = _timestamp_from_datetime(persisted.expires_at)
                    last_seen_at = _timestamp_from_datetime(persisted.last_seen_at)
                    if expires_at is not None and expires_at > now:
                        derived_csrf_token = self._csrf_token(token)
                        session = _Session(
                            csrf_token=derived_csrf_token,
                            csrf_digest=hashlib.sha256(
                                derived_csrf_token.encode("ascii")
                            ).digest(),
                            expires_at=expires_at,
                            last_seen_at=last_seen_at or now,
                        )
                        self._sessions[token_hash] = session
            if session is None or session.expires_at <= now:
                _session_failed()
            if require_csrf:
                if not isinstance(csrf_token, str) or not csrf_token:
                    raise SecurityError(
                        "CSRF_INVALID", "CSRF 校验失败。", status_code=403
                    )
                candidate = hashlib.sha256(csrf_token.encode("utf-8")).digest()
                if not hmac.compare_digest(candidate, session.csrf_digest):
                    raise SecurityError(
                        "CSRF_INVALID", "CSRF 校验失败。", status_code=403
                    )
            old_expiry = session.expires_at
            session.last_seen_at = now
            session.expires_at = now + self.session_seconds
            if self._session_store is not None:
                self._session_store.save_admin_session(
                    token_hash.hex(),
                    datetime.fromtimestamp(session.expires_at, tz=UTC),
                    datetime.fromtimestamp(now, tz=UTC),
                )
            return SessionCredentials(
                token=token,
                csrf_token=session.csrf_token,
                expires_at=datetime.fromtimestamp(session.expires_at, tz=UTC),
                renewed=session.expires_at > old_expiry,
            )

    def revoke(self, token: str | None) -> None:
        if not isinstance(token, str) or not token:
            return
        with self._lock:
            token_hash = self._token_digest(token)
            self._sessions.pop(token_hash, None)
            if self._session_store is not None:
                self._session_store.delete_admin_session(token_hash.hex())

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()
            if self._session_store is not None:
                self._session_store.delete_all_admin_sessions()

    def prune(self) -> None:
        with self._lock:
            self._prune_locked(self._clock(), force=True)

    def _token_digest(self, token: str) -> bytes:
        return hmac.new(
            self._token_hash_key, token.encode("utf-8"), hashlib.sha256
        ).digest()

    def _csrf_token(self, token: str) -> str:
        return base64.urlsafe_b64encode(
            hmac.new(self._csrf_key, token.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")

    def _prune_locked(self, now: float, *, force: bool = False) -> None:
        self._sessions = {
            token_hash: session
            for token_hash, session in self._sessions.items()
            if session.expires_at > now
        }
        if self._session_store is None:
            return
        # Cleanup is maintenance work; running a DELETE for every authenticated
        # request makes a transient database outage take down the entire admin
        # API.  Keep it bounded to once per minute while retaining an explicit
        # ``prune()`` method for startup/shutdown jobs that need an immediate run.
        if not force and self._last_prune_at is not None:
            if now - self._last_prune_at < self.prune_interval_seconds:
                return
        self._last_prune_at = now
        self._session_store.delete_expired_admin_sessions(
            datetime.fromtimestamp(now, tz=UTC)
        )


class FailureRateLimiter:
    """Fixed-window failure limiter keyed by the resolved source IP."""

    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_seconds: int = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_failures <= 0 or window_seconds <= 0:
            raise ValueError("rate limit values must be positive")
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def check(self, source_ip: str) -> None:
        key = _canonical_ip(source_ip)
        with self._lock:
            now = self._clock()
            failures = self._failures[key]
            self._discard_expired(failures, now)
            if len(failures) >= self.max_failures:
                retry_after = max(1, math.ceil(failures[0] + self.window_seconds - now))
                raise SecurityError(
                    "RATE_LIMITED",
                    "请求过于频繁，请稍后重试。",
                    status_code=429,
                    retry_after=retry_after,
                )

    def record_failure(self, source_ip: str) -> None:
        key = _canonical_ip(source_ip)
        with self._lock:
            now = self._clock()
            failures = self._failures[key]
            self._discard_expired(failures, now)
            failures.append(now)

    def record_success(self, source_ip: str) -> None:
        """Clear failures after a successful administrator verification."""

        key = _canonical_ip(source_ip)
        with self._lock:
            self._failures.pop(key, None)

    def _discard_expired(self, failures: deque[float], now: float) -> None:
        threshold = now - self.window_seconds
        while failures and failures[0] <= threshold:
            failures.popleft()


def resolve_client_ip(
    peer_ip: str,
    forwarded_for: str | None,
    trusted_proxies: Sequence[str] | Iterable[str] = (),
) -> str:
    """Resolve X-Forwarded-For only through explicitly trusted proxy hops."""

    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return "invalid-source"
    networks = tuple(
        ipaddress.ip_network(item, strict=False) for item in trusted_proxies
    )
    if not forwarded_for or not _is_in_networks(peer, networks):
        return peer.compressed
    try:
        forwarded = [
            ipaddress.ip_address(item.strip()) for item in forwarded_for.split(",")
        ]
        if not forwarded:
            return peer.compressed
    except ValueError:
        return peer.compressed

    candidate = peer
    for hop in reversed(forwarded):
        if not _is_in_networks(candidate, networks):
            break
        candidate = hop
    return candidate.compressed


def _canonical_ip(value: str) -> str:
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        # The HTTP layer normally supplies a validated socket address.  A
        # stable non-IP bucket is still safer than bypassing the limiter.
        return "invalid-source"


def _is_in_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    return any(
        address.version == network.version and address in network
        for network in networks
    )


__all__ = [
    "ADMIN_KEY_MIN_BYTES",
    "DEFAULT_NONCE_TTL_SECONDS",
    "DEFAULT_TIMESTAMP_SKEW_SECONDS",
    "FailureRateLimiter",
    "SecurityError",
    "SessionCredentials",
    "SessionManager",
    "SessionStore",
    "TransportKeyManager",
    "resolve_client_ip",
    "validate_admin_key",
]
