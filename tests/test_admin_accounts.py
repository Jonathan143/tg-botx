from __future__ import annotations

import asyncio
from collections import deque
from datetime import timedelta
from types import SimpleNamespace

import pytest
from telethon.errors import SessionPasswordNeededError

from tg_botx.interfaces.admin.admin_accounts import AdminAccountError, LoginFlowManager
from tg_botx.config import Settings
from tg_botx.infrastructure.persistence.db import Account, Database, Task, utc_now


class FakeQrLogin:
    def __init__(self, url: str, result=None):
        self.url = url
        self.expires = utc_now() + timedelta(minutes=1)
        self._result = result

    async def wait(self):
        if isinstance(self._result, BaseException):
            raise self._result
        if isinstance(self._result, asyncio.Event):
            await self._result.wait()
        return self._result


class FakeTelegramClient:
    def __init__(self, *, authorized=False, user=None, qr_logins=()):
        self.authorized = authorized
        self.user = user or SimpleNamespace(phone="15500001234")
        self.qr_logins = deque(qr_logins)
        self.connected = False
        self.disconnect_count = 0
        self.logged_out = False
        self.sent_phone = None
        self.code_requires_password = False
        self.last_code = None
        self.last_password = None

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False
        self.disconnect_count += 1

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return self.user

    async def qr_login(self):
        return self.qr_logins.popleft()

    async def send_code_request(self, phone):
        self.sent_phone = phone
        return SimpleNamespace(phone_code_hash="opaque")

    async def sign_in(self, *, code=None, password=None):
        if code is not None:
            self.last_code = code
            if self.code_requires_password:
                raise SessionPasswordNeededError(request=None)
        if password is not None:
            self.last_password = password
        self.authorized = True
        return self.user

    async def log_out(self):
        self.logged_out = True
        self.authorized = False


class ClientFactory:
    def __init__(self, *clients):
        self.clients = deque(clients)
        self.calls = []

    def __call__(self, session_path, api_id, api_hash):
        self.calls.append((session_path, api_id, api_hash))
        return self.clients.popleft()


@pytest.fixture
def manager_parts(tmp_path):
    settings = Settings(api_id=12345, api_hash="test-hash", data_dir=tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    database.create_all()
    return settings, database


def test_phone_flow_is_single_per_account_and_persists_account(manager_parts):
    async def scenario():
        settings, database = manager_parts
        client = FakeTelegramClient()
        manager = LoginFlowManager(settings, database, client_factory=ClientFactory(client))

        created = await manager.start("primary", "phone")
        assert created.stage == "phone_required"
        with pytest.raises(AdminAccountError) as conflict:
            await manager.start("primary", "phone")
        assert conflict.value.code == "LOGIN_FLOW_CONFLICT"

        sent = await manager.submit_phone(created.flow_id, "+15500001234")
        assert sent.stage == "code_pending"
        assert client.sent_phone == "+15500001234"
        verified = await manager.submit_code(created.flow_id, "24680")

        assert verified.stage == "completed"
        assert verified.account_id is not None
        account = database.get_account("primary")
        assert account is not None
        assert account.is_active is True
        assert account.phone == "15500001234"
        assert client.disconnect_count == 1
        # Sensitive inputs are not copied into either public or internal flow state.
        flow = manager._flows_by_id[created.flow_id]
        assert "+15500001234" not in repr(flow)
        assert "24680" not in repr(flow)

    asyncio.run(scenario())


def test_phone_flow_supports_two_factor_stage(manager_parts):
    async def scenario():
        settings, database = manager_parts
        client = FakeTelegramClient()
        client.code_requires_password = True
        manager = LoginFlowManager(settings, database, client_factory=ClientFactory(client))

        created = await manager.start("two-factor", "phone")
        await manager.submit_phone(created.flow_id, "+15500009999")
        waiting = await manager.submit_code(created.flow_id, "13579")
        assert waiting.stage == "password_pending"

        completed = await manager.submit_password(created.flow_id, "never-store-this")
        assert completed.stage == "completed"
        assert client.last_password == "never-store-this"
        assert "never-store-this" not in repr(manager._flows_by_id[created.flow_id])

    asyncio.run(scenario())


def test_qr_timeout_refreshes_url_and_completes(manager_parts):
    async def scenario():
        settings, database = manager_parts
        scanned = asyncio.Event()
        first = FakeQrLogin("tg://login?token=first", asyncio.TimeoutError())
        second = FakeQrLogin("tg://login?token=second", scanned)
        client = FakeTelegramClient(qr_logins=(first, second))
        manager = LoginFlowManager(settings, database, client_factory=ClientFactory(client))

        created = await manager.start("qr-account", "qr")
        assert created.qr_url == "tg://login?token=first"
        for _ in range(10):
            refreshed = await manager.get_flow(created.flow_id)
            if refreshed.qr_url == "tg://login?token=second":
                break
            await asyncio.sleep(0)
        assert refreshed.stage == "qr_pending"
        assert refreshed.qr_url == "tg://login?token=second"
        assert refreshed.qr_expires_at is not None

        scanned.set()
        for _ in range(10):
            completed = await manager.get_flow(created.flow_id)
            if completed.stage == "completed":
                break
            await asyncio.sleep(0)
        assert completed.stage == "completed"
        assert database.get_account("qr-account") is not None

    asyncio.run(scenario())


def test_cancel_disconnects_and_removes_flow(manager_parts):
    async def scenario():
        settings, database = manager_parts
        client = FakeTelegramClient()
        manager = LoginFlowManager(settings, database, client_factory=ClientFactory(client))
        created = await manager.start("cancel-me", "phone")

        await manager.cancel(created.flow_id)

        assert client.disconnect_count == 1
        with pytest.raises(AdminAccountError) as missing:
            await manager.get_flow(created.flow_id)
        assert missing.value.code == "LOGIN_FLOW_NOT_FOUND"

    asyncio.run(scenario())


def test_logout_reports_tasks_and_refuses_enabled_task(manager_parts):
    async def scenario():
        settings, database = manager_parts
        account = database.save_account(
            Account(
                name="logout-account",
                phone="15500001234",
                session_name="logout-account",
                is_active=True,
            )
        )
        task = database.save_task(
            Task(
                account_id=account.id,
                name="enabled-task",
                target="target",
                schedule_type="fixed",
                fixed_time="08:00",
                config_json="{}",
                enabled=True,
            )
        )
        logout_client = FakeTelegramClient(authorized=True)
        manager = LoginFlowManager(settings, database, client_factory=ClientFactory(logout_client))

        impact = manager.logout_impact(account.id)
        assert impact.enabled_task_ids == (task.id,)
        with pytest.raises(AdminAccountError) as blocked:
            await manager.logout(account.id)
        assert blocked.value.code == "ACCOUNT_HAS_ENABLED_TASKS"
        assert logout_client.connected is False

        database.update_task(task.id, enabled=False)
        result = await manager.logout(account.id)
        assert result.enabled_task_ids == ()
        assert logout_client.logged_out is True
        assert database.get_account_by_id(account.id).is_active is False

    asyncio.run(scenario())


def test_account_list_masks_phone(manager_parts):
    settings, database = manager_parts
    database.save_account(
        Account(name="masked", phone="15512345678", session_name="masked", is_active=True)
    )
    manager = LoginFlowManager(
        settings, database, client_factory=ClientFactory(FakeTelegramClient())
    )

    [account] = manager.list_accounts()
    assert account.phone_masked == "155******78"
    assert "123456" not in repr(account)
