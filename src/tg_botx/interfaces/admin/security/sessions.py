from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from tg_botx.interfaces.admin.security.primitives import (
    SecurityError,
    _random_token,
    _session_failed,
    _timestamp_from_datetime,
    validate_admin_key,
)


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

    def save_admin_session(
        self, token_hash: str, expires_at: datetime, last_seen_at: datetime
    ) -> None: ...

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
        self._csrf_key = hmac.new(key, b"tg-bot-admin-session-csrf-v1", hashlib.sha256).digest()
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
                            csrf_digest=hashlib.sha256(derived_csrf_token.encode("ascii")).digest(),
                            expires_at=expires_at,
                            last_seen_at=last_seen_at or now,
                        )
                        self._sessions[token_hash] = session
            if session is None or session.expires_at <= now:
                _session_failed()
            if require_csrf:
                if not isinstance(csrf_token, str) or not csrf_token:
                    raise SecurityError("CSRF_INVALID", "CSRF 校验失败。", status_code=403)
                candidate = hashlib.sha256(csrf_token.encode("utf-8")).digest()
                if not hmac.compare_digest(candidate, session.csrf_digest):
                    raise SecurityError("CSRF_INVALID", "CSRF 校验失败。", status_code=403)
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
        return hmac.new(self._token_hash_key, token.encode("utf-8"), hashlib.sha256).digest()

    def _csrf_token(self, token: str) -> str:
        return (
            base64.urlsafe_b64encode(
                hmac.new(self._csrf_key, token.encode("utf-8"), hashlib.sha256).digest()
            )
            .decode("ascii")
            .rstrip("=")
        )

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
        if (
            not force
            and self._last_prune_at is not None
            and now - self._last_prune_at < self.prune_interval_seconds
        ):
            return
        self._last_prune_at = now
        self._session_store.delete_expired_admin_sessions(datetime.fromtimestamp(now, tz=UTC))
