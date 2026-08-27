from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from tg_botx.interfaces.admin.admin_security import (
    FailureRateLimiter,
    SecurityError,
    SessionManager,
    TransportKeyManager,
    resolve_client_ip,
    validate_admin_key,
)

ADMIN_KEY = "db3BvR9P8y6F0HcXe5i7qL2sNu4mKa1ZpT8wJfGx"


class FakeClock:
    def __init__(self, value: float = 1_800_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def encrypt(challenge: dict[str, str], payload: dict[str, object]) -> str:
    public_key = serialization.load_pem_public_key(
        challenge["publicKey"].encode("ascii")
    )
    ciphertext = public_key.encrypt(
        json.dumps(payload, separators=(",", ":")).encode(),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode("ascii")


def auth_ciphertext(
    challenge: dict[str, str],
    clock: FakeClock,
    *,
    key: str = ADMIN_KEY,
    timestamp: float | None = None,
) -> str:
    return encrypt(
        challenge,
        {
            "value": key,
            "nonce": challenge["nonce"],
            "timestamp": (
                datetime.fromtimestamp(clock(), tz=UTC)
                .isoformat()
                .replace("+00:00", "Z")
                if timestamp is None
                else str(timestamp)
            ),
        },
    )


def assert_security_error(code: str, callback) -> SecurityError:
    with pytest.raises(SecurityError) as raised:
        callback()
    assert raised.value.code == code
    return raised.value


def test_admin_key_rejects_short_and_obvious_placeholder() -> None:
    assert validate_admin_key(ADMIN_KEY) == ADMIN_KEY.encode()
    assert_security_error("ADMIN_KEY_INVALID", lambda: validate_admin_key("short"))
    assert_security_error("ADMIN_KEY_INVALID", lambda: validate_admin_key("x" * 64))


def test_rsa_oaep_decrypts_verifies_and_consumes_nonce() -> None:
    clock = FakeClock()
    manager = TransportKeyManager(clock=clock)
    challenge = manager.issue_challenge("admin")
    ciphertext = auth_ciphertext(challenge, clock)

    payload = manager.verify_admin_payload(challenge["keyId"], ciphertext, ADMIN_KEY)
    assert payload["value"] == ADMIN_KEY
    assert "BEGIN PUBLIC KEY" in challenge["publicKey"]
    assert challenge["algorithm"] == "RSA-OAEP-256"

    error = assert_security_error(
        "AUTH_FAILED",
        lambda: manager.verify_admin_payload(challenge["keyId"], ciphertext, ADMIN_KEY),
    )
    assert error.message == "认证失败。"


def test_wrong_key_stale_timestamp_and_wrong_purpose_are_uniform_failures() -> None:
    clock = FakeClock()
    manager = TransportKeyManager(clock=clock)

    challenge = manager.issue_challenge("admin")
    assert_security_error(
        "AUTH_FAILED",
        lambda: manager.verify_admin_payload(
            challenge["keyId"],
            auth_ciphertext(
                challenge, clock, key="Pm8nQw3xZa6vFd1rTy9kLc4jHg7sBe2uIo5pXs0A"
            ),
            ADMIN_KEY,
        ),
    )

    challenge = manager.issue_challenge("admin")
    assert_security_error(
        "AUTH_FAILED",
        lambda: manager.verify_admin_payload(
            challenge["keyId"],
            auth_ciphertext(challenge, clock, timestamp=(clock() - 121) * 1000),
            ADMIN_KEY,
        ),
    )

    challenge = manager.issue_challenge("telegram_phone")
    ciphertext = encrypt(
        challenge,
        {"phone": "+12025550123", "nonce": challenge["nonce"], "timestamp": clock()},
    )
    assert_security_error(
        "AUTH_FAILED",
        lambda: manager.decrypt_payload(
            challenge["keyId"], ciphertext, "telegram_code"
        ),
    )
    # Purpose mismatches consume the nonce instead of allowing an oracle retry.
    assert_security_error(
        "AUTH_FAILED",
        lambda: manager.decrypt_payload(
            challenge["keyId"], ciphertext, "telegram_phone"
        ),
    )


def test_admin_payload_rejects_field_aliases_extra_fields_and_non_string_timestamp() -> (
    None
):
    clock = FakeClock()
    manager = TransportKeyManager(clock=clock)
    invalid_payloads = [
        {"key": ADMIN_KEY, "nonce": None, "timestamp": str(clock())},
        {"adminKey": ADMIN_KEY, "nonce": None, "timestamp": str(clock())},
        {"value": ADMIN_KEY, "nonce": None, "timestamp": str(clock()), "extra": True},
        {"value": ADMIN_KEY, "nonce": None, "timestamp": clock()},
    ]
    for payload in invalid_payloads:
        challenge = manager.issue_challenge("admin")
        payload["nonce"] = challenge["nonce"]
        ciphertext = encrypt(challenge, payload)
        assert_security_error(
            "AUTH_FAILED",
            lambda challenge=challenge, ciphertext=ciphertext: (
                manager.verify_admin_payload(challenge["keyId"], ciphertext, ADMIN_KEY)
            ),
        )


def test_all_sensitive_payloads_require_the_same_exact_shape() -> None:
    clock = FakeClock()
    manager = TransportKeyManager(clock=clock)
    for payload in (
        {"phone": "+12025550123", "nonce": None, "timestamp": str(clock())},
        {"value": "24680", "nonce": None, "timestamp": str(clock()), "extra": True},
        {"value": "secret", "nonce": None, "timestamp": clock()},
    ):
        challenge = manager.issue_challenge("phone")
        payload["nonce"] = challenge["nonce"]
        ciphertext = encrypt(challenge, payload)
        assert_security_error(
            "AUTH_FAILED",
            lambda challenge=challenge, ciphertext=ciphertext: manager.decrypt_payload(
                challenge["keyId"], ciphertext, "phone"
            ),
        )


def test_nonce_expires() -> None:
    clock = FakeClock()
    manager = TransportKeyManager(clock=clock, nonce_ttl_seconds=10)
    challenge = manager.issue_challenge("admin")
    ciphertext = auth_ciphertext(challenge, clock)
    clock.advance(11)
    assert_security_error(
        "AUTH_FAILED",
        lambda: manager.verify_admin_payload(challenge["keyId"], ciphertext, ADMIN_KEY),
    )


def test_rotation_retains_old_private_key_only_for_grace_period() -> None:
    clock = FakeClock()
    manager = TransportKeyManager(
        clock=clock,
        old_key_grace_seconds=5,
        nonce_ttl_seconds=30,
    )
    first = manager.issue_challenge("admin")
    first_ciphertext = auth_ciphertext(first, clock)
    new_key_id = manager.rotate_now()
    assert new_key_id != first["keyId"]

    manager.verify_admin_payload(first["keyId"], first_ciphertext, ADMIN_KEY)

    second_old = manager.issue_challenge("admin")
    second_ciphertext = auth_ciphertext(second_old, clock)
    manager.rotate_now()
    clock.advance(6)
    assert_security_error(
        "AUTH_FAILED",
        lambda: manager.verify_admin_payload(
            second_old["keyId"], second_ciphertext, ADMIN_KEY
        ),
    )


def test_rotation_happens_automatically_when_due() -> None:
    clock = FakeClock()
    manager = TransportKeyManager(clock=clock, rotation_hours=1)
    old_id = manager.current_key_id
    clock.advance(3601)
    assert manager.rotate_if_due()
    assert manager.current_key_id != old_id


def test_session_is_random_csrf_protected_and_rolling() -> None:
    clock = FakeClock()
    manager = SessionManager(ADMIN_KEY, session_days=1, clock=clock)
    first = manager.create()
    second = manager.create()
    assert first.token != second.token
    assert first.csrf_token != second.csrf_token

    manager.authenticate(first.token)
    assert_security_error(
        "CSRF_INVALID",
        lambda: manager.authenticate(first.token, "wrong", require_csrf=True),
    )
    clock.advance(60)
    state = manager.authenticate(first.token, first.csrf_token, require_csrf=True)
    assert state.renewed

    # Rolling access extends the original one-day deadline.
    clock.advance(86_400 - 30)
    manager.authenticate(first.token)
    manager.revoke(first.token)
    assert_security_error("SESSION_INVALID", lambda: manager.authenticate(first.token))


def test_sessions_do_not_survive_a_restart_or_admin_key_change() -> None:
    first = SessionManager(ADMIN_KEY)
    credentials = first.create()
    restarted = SessionManager(ADMIN_KEY)
    changed = SessionManager("Nm2vYq7tKx4sGa9pWc1jLf8uRh5eZd3bIo6nHs0Q")
    assert_security_error(
        "SESSION_INVALID", lambda: restarted.authenticate(credentials.token)
    )
    assert_security_error(
        "SESSION_INVALID", lambda: changed.authenticate(credentials.token)
    )


def test_failure_rate_limiter_allows_five_failures_per_window() -> None:
    clock = FakeClock()
    limiter = FailureRateLimiter(clock=clock)
    for _ in range(5):
        limiter.check("203.0.113.7")
        limiter.record_failure("203.0.113.7")

    error = assert_security_error("RATE_LIMITED", lambda: limiter.check("203.0.113.7"))
    assert error.status_code == 429
    assert error.retry_after == 600

    # Other sources are independent, and the bucket expires after ten minutes.
    limiter.check("203.0.113.8")
    clock.advance(600)
    limiter.check("203.0.113.7")


def test_success_clears_failure_bucket() -> None:
    limiter = FailureRateLimiter()
    for _ in range(5):
        limiter.record_failure("203.0.113.7")
    limiter.record_success("203.0.113.7")
    limiter.check("203.0.113.7")


def test_forwarded_for_is_used_only_for_explicitly_trusted_proxies() -> None:
    assert (
        resolve_client_ip("198.51.100.9", "203.0.113.7", ["10.0.0.0/8"])
        == "198.51.100.9"
    )
    assert resolve_client_ip("10.0.0.3", "203.0.113.7", ["10.0.0.0/8"]) == "203.0.113.7"
    assert (
        resolve_client_ip(
            "10.0.0.3",
            "203.0.113.7, 10.0.0.2",
            ["10.0.0.0/8"],
        )
        == "203.0.113.7"
    )
    assert resolve_client_ip("10.0.0.3", "not-an-ip", ["10.0.0.0/8"]) == "10.0.0.3"
