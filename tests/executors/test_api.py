import base64
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient

from tg_botx.config import Settings
from tg_botx.features.bot.execution import CommandAdmissionUnavailable
from tg_botx.features.checkin.runtime import CheckinService
from tg_botx.integrations.telegram_bot import TelegramBotApiClient
from tg_botx.interfaces.admin.admin_api import create_admin_app
from tg_botx.interfaces.telegram.runtime import TelegramManagementBot

ORIGIN = "https://admin.example.test"
KEY = "db3BvR9P8y6F0HcXe5i7qL2sNu4mKa1ZpT8wJfGx"  # Test-only fixture, not a real credential.
ECHO = {
    "executorType": "builtin_function",
    "executorConfig": {"function": "echo", "arguments": {"text": "{{ argument }}"}},
}


@pytest.fixture
def api(db, tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        admin_key=KEY,
        admin_origin=ORIGIN,
        bot_enabled=False,
        admin_bot_token=None,
        notification_bot_token=None,
        service_lifecycle_notifications_enabled=False,
    )
    settings.ensure_directories()
    app = create_admin_app(settings, db, CheckinService(settings, db))
    with TestClient(app, base_url=ORIGIN) as client:
        challenge = client.get("/api/auth/key?purpose=admin").json()
        key = serialization.load_pem_public_key(challenge["publicKey"].encode())
        data = json.dumps(
            {"value": KEY, "nonce": challenge["nonce"], "timestamp": datetime.now(UTC).isoformat()}
        ).encode()
        ciphertext = base64.b64encode(
            key.encrypt(
                data,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
        ).decode()
        login = client.post(
            "/api/auth/verify",
            headers={"Origin": ORIGIN},
            json={"keyId": challenge["keyId"], "ciphertext": ciphertext},
        )
        assert login.status_code == 200
        client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrfToken"]})
        yield client, app


def test_capabilities_validation_and_crud_contract(api):
    client, app = api
    response = client.get("/api/bot/executors")
    assert response.status_code == 200
    assert [item["type"] for item in response.json()["executors"]] == [
        "http",
        "builtin_function",
        "python",
    ]
    assert response.json()["removedTypes"] == ["javascript"]
    assert "my_points" in {
        item["name"] for item in client.get("/api/bot/builtin-functions").json()["functions"]
    }
    validation = client.post("/api/bot/command-validation", json=ECHO)
    assert validation.json()["valid"] and validation.json()["canEnable"]
    response = client.post(
        "/api/bot/commands",
        json={"command": "echo", "description": "回声", "enabled": True, **ECHO},
    )
    assert response.status_code == 201 and response.json()["effectiveEnabled"]
    revision = response.json()["revision"]
    response = client.patch(
        "/api/bot/commands/echo",
        json={"executorConfig": {"function": "utc_time"}, "expectedRevision": revision},
    )
    assert (
        response.status_code == 200 and response.json()["executorConfig"]["function"] == "utc_time"
    )
    response = client.patch(
        "/api/bot/commands/echo", json={"command": "rename", "expectedRevision": revision}
    )
    assert response.status_code == 409
    response = client.patch("/api/bot/commands/echo", json={"executorConfig": None})
    assert response.status_code == 422
    for kind in ["javascript", "js", "os.system"]:
        response = client.post(
            "/api/bot/commands",
            json={"command": "bad", "description": "拒绝", "executorType": kind},
        )
        assert response.status_code == 422
    assert "lastExecution" in next(
        item
        for item in client.get("/api/bot/commands").json()["commands"]
        if item["command"] == "echo"
    )


def test_test_endpoint_reuses_execution_and_is_idempotent(api):
    client, app = api
    request = {**ECHO, "argument": "actual execution"}
    response = client.post(
        "/api/bot/command-tests", json=request, headers={"Idempotency-Key": "same-request"}
    )
    assert response.status_code == 202
    execution_id = response.json()["executionId"]
    duplicate = client.post(
        "/api/bot/command-tests", json=request, headers={"Idempotency-Key": "same-request"}
    )
    assert duplicate.json()["executionId"] == execution_id and not duplicate.json()["created"]
    changed = client.post(
        "/api/bot/command-tests",
        json={**request, "argument": "different"},
        headers={"Idempotency-Key": "same-request"},
    )
    assert changed.status_code == 409
    deadline = time.monotonic() + 5
    while True:
        result = client.get(f"/api/bot/command-executions/{execution_id}").json()
        if result["status"] not in {"queued", "running"}:
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert result["status"] == "succeeded" and result["result"]["text"] == "actual execution"
    assert result["deliveryStatus"] == "skipped"
    assert client.get("/api/bot/command-executions").json()["executions"]
    assert client.get("/api/bot/command-executions/not-found").status_code == 404


def test_execution_confirmation_auth_csrf_and_body_limit(api):
    client, app = api
    for kind, config in [
        ("http", {"url": "https://api.example.test"}),
        ("python", {"code": 'def main(ctx):\n    return {"text":"x"}'}),
    ]:
        response = client.post(
            "/api/bot/command-tests", json={"executorType": kind, "executorConfig": config}
        )
        assert (
            response.status_code == 422
            and response.json()["error"]["code"] == "EXECUTION_CONFIRMATION_REQUIRED"
        )
    assert (
        client.post(
            "/api/bot/command-tests", json={**ECHO, "userId": 1, "role": "admin"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/bot/command-validation",
            content=b"x" * 65537,
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/api/bot/command-tests", json=ECHO, headers={"X-CSRF-Token": "invalid"}
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get("/api/bot/executors").status_code == 401
    assert client.post("/api/bot/command-tests", json=ECHO).status_code == 401


async def test_webhook_does_not_ack_failed_durable_admission():
    bot = object.__new__(TelegramManagementBot)
    bot.status = SimpleNamespace(last_poll_at=None, last_error=None)
    bot.handlers = SimpleNamespace(
        _handle_update=AsyncMock(
            side_effect=[CommandAdmissionUnavailable("storage unavailable"), None]
        )
    )
    update = {"update_id": 123}
    with pytest.raises(CommandAdmissionUnavailable):
        await bot.handle_webhook_update(update)
    await bot.handle_webhook_update(update)
    assert bot.handlers._handle_update.await_count == 2
    await bot.handle_webhook_update(update)
    assert bot.handlers._handle_update.await_count == 2


async def test_telegram_plaintext_reply_omits_parse_mode():
    client = object.__new__(TelegramBotApiClient)
    client.call = AsyncMock(return_value={})
    await client.send_message(123, "<b>& plain", parse_mode=None)
    assert "parse_mode" not in client.call.await_args.args[1]
