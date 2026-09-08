from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence

from tg_botx.interfaces.admin.security.primitives import SecurityError


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
    networks = tuple(ipaddress.ip_network(item, strict=False) for item in trusted_proxies)
    if not forwarded_for or not _is_in_networks(peer, networks):
        return peer.compressed
    try:
        forwarded = [ipaddress.ip_address(item.strip()) for item in forwarded_for.split(",")]
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
    return any(address.version == network.version and address in network for network in networks)
