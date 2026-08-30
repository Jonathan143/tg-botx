from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient
from starlette.requests import Request

from tg_botx.interfaces.admin.admin_api import create_admin_app
from tg_botx.interfaces.admin.admin_accounts import LogoutImpact
from tg_botx.config import Settings
from tg_botx.infrastructure.persistence.db import Account, Database, Task, TaskRun, utc_now
from tg_botx.features.checkin.runtime import CheckinService


ADMIN_KEY = "db3BvR9P8y6F0HcXe5i7qL2sNu4mKa1ZpT8wJfGx"
ORIGIN = "https://admin.example.test"


def encrypted_value(challenge: dict[str, str], value: str) -> str:
    public_key = serialization.load_pem_public_key(challenge["publicKey"].encode("ascii"))
    plaintext = json.dumps(
        {
            "value": value,
            "nonce": challenge["nonce"],
            "timestamp": datetime.now(UTC).isoformat(),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = public_key.encrypt(
        plaintext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode("ascii")


def app_for(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        admin_key=ADMIN_KEY,
        admin_origin=ORIGIN,
        log_file="admin-test.log",
    )
    settings.ensure_directories()
    database = Database(f"sqlite:///{tmp_path / 'admin-test.sqlite3'}")
    database.create_all()
    database.save_account(Account(name="default", session_name="default"))
    service = CheckinService(settings, database)
    return create_admin_app(settings, database, service)


def authenticate(client: TestClient, *, key: str = ADMIN_KEY):
    challenge = client.get("/api/auth/key?purpose=admin").json()
    ciphertext = encrypted_value(challenge, key)
    return client.post(
        "/api/auth/verify",
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        json={"keyId": challenge["keyId"], "ciphertext": ciphertext},
    )


def test_session_contract_origin_csrf_and_protected_openapi(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        unauthorized = client.get("/api/auth/session")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "AUTH_REQUIRED"
        invalid = client.get(
            "/api/auth/session",
            headers={"Cookie": "tg_bot_admin_session=invalid-session"},
        )
        assert invalid.status_code == 401
        assert invalid.json()["error"]["code"] == "AUTH_REQUIRED"
        assert client.get("/api/openapi.json").status_code == 401

        verified = authenticate(client)
        assert verified.status_code == 200
        body = verified.json()
        assert set(body) == {"authenticated", "csrfToken", "sessionExpiresAt"}
        assert body["authenticated"] is True
        assert "HttpOnly" in verified.headers["set-cookie"]
        assert "Secure" in verified.headers["set-cookie"]
        assert "SameSite=strict" in verified.headers["set-cookie"]

        session = client.get("/api/auth/session")
        assert session.status_code == 200
        assert set(session.json()) == {"authenticated", "csrfToken", "sessionExpiresAt"}
        assert session.json()["csrfToken"] == body["csrfToken"]
        assert client.get("/api/openapi.json").status_code == 200

        wrong_origin = client.post(
            "/api/auth/logout",
            headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
            json={},
        )
        assert wrong_origin.status_code == 403
        assert wrong_origin.json()["error"]["code"] == "ORIGIN_FORBIDDEN"

        missing_csrf = client.post(
            "/api/auth/logout",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            json={},
        )
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["error"]["code"] == "CSRF_INVALID"

        logged_out = client.post(
            "/api/auth/logout",
            headers={
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "X-CSRF-Token": body["csrfToken"],
            },
            json={},
        )
        assert logged_out.status_code == 204
        assert client.get("/api/auth/session").status_code == 401


def test_admin_verify_rate_limit_and_uniform_error(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        for _ in range(5):
            response = authenticate(client, key="Rq8mTv2zLd7cXs4nHa9pJf6wKe3uBg5yNi1oPc0V")
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "AUTH_FAILED"

        limited = authenticate(client, key="Rq8mTv2zLd7cXs4nHa9pJf6wKe3uBg5yNi1oPc0V")
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert int(limited.headers["retry-after"]) > 0


def test_mutation_requires_json_and_transport_key_can_rotate(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        verified = authenticate(client).json()
        headers = {"Origin": ORIGIN, "X-CSRF-Token": verified["csrfToken"]}

        not_json = client.post(
            "/api/settings/transport-key/rotate", headers=headers, content=b""
        )
        assert not_json.status_code == 415
        assert not_json.json()["error"]["code"] == "JSON_REQUIRED"

        before = client.get("/api/auth/key?purpose=admin").json()["keyId"]
        rotated = client.post(
            "/api/settings/transport-key/rotate",
            headers={**headers, "Content-Type": "application/json"},
            json={},
        )
        assert rotated.status_code == 200
        assert set(rotated.json()) == {
            "keyId",
            "rotatedAt",
            "previousKeyExpiresAt",
        }
        assert rotated.json()["keyId"] != before


def test_task_crud_actions_and_yaml_preflight_use_live_scheduler(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        verified = authenticate(client).json()
        headers = {
            "Origin": ORIGIN,
            "Content-Type": "application/json",
            "X-CSRF-Token": verified["csrfToken"],
        }
        definition = {
            "name": "api-daily",
            "account": "default",
            "target": "checkin_bot",
            "schedule": {"type": "fixed", "timezone": "UTC", "time": "23:59:00"},
            "steps": [{"type": "send_message", "text": "/checkin"}],
        }

        created = client.post("/api/tasks", headers=headers, json={"definition": definition})
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert created.json()["enabled"] is False

        published = client.post(f"/api/tasks/{task_id}/publish", headers=headers, json={})
        assert published.status_code == 200

        enabled = client.post(f"/api/tasks/{task_id}/enable", headers=headers, json={})
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert enabled.json()["nextRunAt"] is not None

        definition["schedule"]["time"] = "01:02:03"
        edited = client.patch(
            f"/api/tasks/{task_id}", headers=headers, json={"definition": definition}
        )
        assert edited.status_code == 200
        assert edited.json()["schedule"]["time"] == "01:02:03"

        archived = client.post(f"/api/tasks/{task_id}/archive", headers=headers, json={})
        assert archived.json()["archived"] is True
        assert archived.json()["enabled"] is False
        assert archived.json()["nextRunAt"] is None

        restored = client.post(f"/api/tasks/{task_id}/restore", headers=headers, json={})
        assert restored.json()["archived"] is False
        assert restored.json()["enabled"] is False

        yaml_text = """\
name: api-daily
account: default
target: checkin_bot
schedule:
  type: fixed
  timezone: UTC
  time: '08:00:00'
steps:
  - type: send_message
    text: /checkin
"""
        preflight = client.post(
            "/api/tasks/import/preflight",
            headers=headers,
            json={"yaml": yaml_text, "overwriteNames": []},
        )
        assert preflight.status_code == 200
        assert preflight.json()["valid"] is True
        assert preflight.json()["conflicts"] == ["api-daily"]

        refused = client.post(
            "/api/tasks/import",
            headers=headers,
            json={"yaml": yaml_text, "overwriteNames": []},
        )
        assert refused.status_code == 409
        overwritten = client.post(
            "/api/tasks/import",
            headers=headers,
            json={"yaml": yaml_text, "overwriteNames": ["api-daily"]},
        )
        assert overwritten.status_code == 200
        assert overwritten.json()["overwritten"] is True


def test_task_json_uses_snake_case_for_create_detail_and_edit(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        verified = authenticate(client).json()
        headers = {
            "Origin": ORIGIN,
            "Content-Type": "application/json",
            "X-CSRF-Token": verified["csrfToken"],
        }
        definition = {
            "name": "camel-task",
            "account": "default",
            "target": "checkin_bot",
            "schedule": {"type": "fixed", "timezone": "UTC", "time": "12:00:00"},
            "retry": {"max_attempts": 2, "backoff_seconds": [1, 2]},
            "steps": [
                {"type": "send_message", "text": "/start"},
                {
                    "type": "wait_message",
                    "timeout_seconds": 45,
                    "success": {"mode": "contains", "value": "ok"},
                },
                {"type": "click_button", "text_contains": "签到"},
            ],
            "log_bot_response": False,
            "notify_bot_response": True,
        }

        created = client.post("/api/tasks", headers=headers, json={"definition": definition})
        assert created.status_code == 201
        task_id = created.json()["id"]
        created_definition = created.json()["definition"]
        assert created_definition["retry"] == {
            "max_attempts": 2,
            "backoff_seconds": [1, 2],
        }
        assert created_definition["steps"][1]["timeout_seconds"] == 45
        assert created_definition["steps"][2]["text_contains"] == "签到"

        detail = client.get(f"/api/tasks/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["definition"]["retry"]["max_attempts"] == 2
        assert detail.json()["definition"]["notify_bot_response"] is True

        definition["retry"]["max_attempts"] = 4
        edited = client.patch(
            f"/api/tasks/{task_id}", headers=headers, json={"definition": definition}
        )
        assert edited.status_code == 200
        assert edited.json()["definition"]["retry"]["max_attempts"] == 4


def test_task_events_require_session_and_send_full_initial_snapshot(
    tmp_path, monkeypatch
):
    app = app_for(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        unauthorized = client.get("/api/tasks/missing/events")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "AUTH_REQUIRED"

        verified = authenticate(client).json()
        headers = {
            "Origin": ORIGIN,
            "Content-Type": "application/json",
            "X-CSRF-Token": verified["csrfToken"],
        }
        definition = {
            "name": "sse-task",
            "account": "default",
            "target": "checkin_bot",
            "schedule": {"type": "fixed", "timezone": "UTC", "time": "12:00:00"},
            "steps": [{"type": "send_message", "text": "/checkin"}],
        }
        created = client.post("/api/tasks", headers=headers, json={"definition": definition})
        task_id = created.json()["id"]
        detail = client.get(f"/api/tasks/{task_id}").json()

        disconnect_checks = iter((False, True))

        async def disconnected(_: Request) -> bool:
            return next(disconnect_checks)

        monkeypatch.setattr(Request, "is_disconnected", disconnected)
        monkeypatch.setattr("tg_botx.interfaces.admin.admin_api.TASK_EVENT_KEEPALIVE_SECONDS", 0.001)
        response = client.get(f"/api/tasks/{task_id}/events")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert response.headers["x-accel-buffering"] == "no"
        lines = response.text.strip().splitlines()
        assert lines[0].startswith("id: ")
        assert int(lines[0].removeprefix("id: ")) > 0
        assert lines[1] == "event: task.updated"
        assert json.loads(lines[2].removeprefix("data: ")) == detail
        assert lines[-1] == ": keepalive"
        assert app.state.checkin_service._task_subscribers == {}


def test_manual_run_returns_running_task_with_initialized_step_statuses(
    tmp_path, monkeypatch
):
    app = app_for(tmp_path)

    async def blocked_run(executor, task):
        await asyncio.sleep(60)

    monkeypatch.setattr("tg_botx.features.checkin.runtime.run_with_retries", blocked_run)
    with TestClient(app, base_url=ORIGIN) as client:
        verified = authenticate(client).json()
        headers = {
            "Origin": ORIGIN,
            "Content-Type": "application/json",
            "X-CSRF-Token": verified["csrfToken"],
        }
        definition = {
            "name": "manual-sse-task",
            "account": "default",
            "target": "checkin_bot",
            "schedule": {"type": "fixed", "timezone": "UTC", "time": "12:00:00"},
            "steps": [
                {"type": "send_message", "text": "/start"},
                {"type": "wait_message", "timeout_seconds": 10},
            ],
        }
        task_id = client.post(
            "/api/tasks", headers=headers, json={"definition": definition}
        ).json()["id"]

        published = client.post(f"/api/tasks/{task_id}/publish", headers=headers, json={})
        assert published.status_code == 200

        response = client.post(f"/api/tasks/{task_id}/run", headers=headers, json={})

        assert response.status_code == 202
        task = response.json()
        assert task["id"] == task_id
        assert task["running"] is True
        assert task["run"]["status"] == "running"
        assert task["run"]["id"]
        assert task["run"]["stepStatuses"] == [
            {"index": 0, "status": "pending"},
            {"index": 1, "status": "pending"},
        ]
        assert "accepted" not in task

        async def disconnected(_: Request) -> bool:
            return True

        monkeypatch.setattr(Request, "is_disconnected", disconnected)
        event_response = client.get(f"/api/tasks/{task_id}/events")
        event_lines = event_response.text.strip().splitlines()
        event_task = json.loads(event_lines[2].removeprefix("data: "))
        assert event_lines[1] == "event: task.updated"
        assert event_task["running"] is True
        assert event_task["run"]["id"] == task["run"]["id"]
        assert event_task["run"]["stepStatuses"] == task["run"]["stepStatuses"]

        canceled = client.post(f"/api/tasks/{task_id}/cancel", headers=headers, json={})
        assert canceled.status_code == 202


def test_dashboard_returns_trends_health_upcoming_tasks_and_account_status(tmp_path):
    app = app_for(tmp_path)
    database = app.state.database
    account = database.get_account("default")
    task = database.save_task(
        Task(
            account_id=account.id,
            name="upcoming",
            target="checkin_bot",
            timezone="UTC",
            schedule_type="fixed",
            fixed_time="23:59:00",
            config_json=json.dumps(
                {
                    "name": "upcoming",
                    "account": "default",
                    "target": "checkin_bot",
                    "schedule": {"type": "fixed", "timezone": "UTC", "time": "23:59:00"},
                    "steps": [{"type": "send_message", "text": "/checkin"}],
                }
            ),
            enabled=True,
            next_run_at=utc_now() + timedelta(hours=2),
        )
    )
    for status, hours_ago in (("success", 1), ("success", 2), ("failed", 3)):
        database.add_run(
            TaskRun(
                task_id=task.id,
                status=status,
                started_at=utc_now() - timedelta(hours=hours_ago),
                finished_at=utc_now() - timedelta(hours=hours_ago) + timedelta(minutes=1),
            )
        )

    with TestClient(app, base_url=ORIGIN) as client:
        assert authenticate(client).status_code == 200

        dashboard = client.get("/api/dashboard?range=24h")
        assert dashboard.status_code == 200
        body = dashboard.json()
        assert body["stats"].items() >= {
            "totalTasks": 1,
            "enabledTasks": 1,
            "runningTasks": 0,
            "failedRuns": 1,
            "successRate": 66.7,
        }.items()
        assert len(body["statusBreakdown"]) == 24
        assert sum(item["success"] for item in body["statusBreakdown"]) == 2
        assert sum(item["failed"] for item in body["statusBreakdown"]) == 1
        assert set(body["health"]) >= {"service", "database", "scheduler", "telegram"}
        assert body["health"]["service"] == "healthy"
        assert body["health"]["database"] == "healthy"
        assert body["health"]["scheduler"] == "healthy"
        assert body["health"]["telegram"] == "healthy"
        assert body["upcomingTasks"][0]["id"] == task.id
        assert body["upcomingTasks"][0]["timezone"] == "UTC"
        assert body["accountStatus"] == [
            {"status": "active", "label": "正常", "count": 1},
            {"status": "inactive", "label": "停用", "count": 0},
        ]

        assert len(client.get("/api/dashboard?range=7d").json()["statusBreakdown"]) == 7
        assert len(client.get("/api/dashboard?range=30d").json()["statusBreakdown"]) == 30


def test_logout_account_invokes_telegram_logout_once(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    account = app.state.database.get_account("default")
    logout = AsyncMock(return_value=LogoutImpact(account.id, account.name, ()))
    monkeypatch.setattr(app.state.login_flows, "logout", logout)

    with TestClient(app, base_url=ORIGIN) as client:
        verified = authenticate(client).json()
        response = client.post(
            f"/api/accounts/{account.id}/logout",
            headers={
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "X-CSRF-Token": verified["csrfToken"],
            },
            json={},
        )
        assert response.status_code == 200
        assert response.json()["loggedOut"] is True
        logout.assert_awaited_once_with(account.id)
