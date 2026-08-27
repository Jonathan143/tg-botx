from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeHashEmptyError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from tg_botx.config import Settings
from tg_botx.infrastructure.persistence.db import Account, Database, utc_now


LoginMethod = Literal["qr", "phone"]
LoginStage = Literal[
    "connecting",
    "phone_required",
    "qr_pending",
    "code_pending",
    "password_pending",
    "completed",
    "failed",
]

_ACTIVE_STAGES = {
    "connecting",
    "phone_required",
    "qr_pending",
    "code_pending",
    "password_pending",
}
_ACCOUNT_NAME = re.compile(r"^[^/\\\x00]{1,100}$")


class AdminAccountError(RuntimeError):
    """An API-safe account-management error with a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class LoginFlowView:
    flow_id: str
    account_name: str
    method: LoginMethod
    stage: LoginStage
    qr_url: str | None = field(repr=False)
    qr_expires_at: datetime | None
    account_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AccountView:
    account_id: str
    name: str
    phone_masked: str | None
    session_name: str
    is_active: bool
    created_at: datetime
    task_count: int
    enabled_task_count: int


@dataclass(frozen=True, slots=True)
class AccountTaskImpact:
    task_id: str
    name: str
    enabled: bool
    archived: bool


@dataclass(frozen=True, slots=True)
class LogoutImpact:
    account_id: str
    account_name: str
    tasks: tuple[AccountTaskImpact, ...]

    @property
    def enabled_task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.tasks if task.enabled)

    @property
    def enabled_task_count(self) -> int:
        return len(self.enabled_task_ids)


@dataclass(slots=True)
class _LoginFlow:
    flow_id: str
    account_name: str
    method: LoginMethod
    stage: LoginStage
    client: Any = field(repr=False)
    connected: bool = False
    qr_url: str | None = field(default=None, repr=False)
    qr_expires_at: datetime | None = None
    account_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    qr_login: Any | None = field(default=None, repr=False)
    waiter: asyncio.Task[None] | None = field(default=None, repr=False)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def view(self) -> LoginFlowView:
        return LoginFlowView(
            flow_id=self.flow_id,
            account_name=self.account_name,
            method=self.method,
            stage=self.stage,
            qr_url=self.qr_url,
            qr_expires_at=self.qr_expires_at,
            account_id=self.account_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class LoginFlowManager:
    """Owns short-lived, in-memory Telegram login flows for the admin API.

    Decrypted phone numbers, verification codes and 2FA passwords are accepted
    only by the method that consumes them.  They are never copied into flow
    state or exception messages.  Telethon retains the minimum phone state it
    needs between ``send_code_request`` and ``sign_in`` inside its client.
    """

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        client_factory: Callable[[str, int, str], Any] = TelegramClient,
        client_pool: Any | None = None,
    ):
        self.settings = settings
        self.database = database
        self._client_factory = client_factory
        self._client_pool = client_pool
        self._flows_by_id: dict[str, _LoginFlow] = {}
        self._flow_ids_by_account: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(self, account_name: str, method: LoginMethod) -> LoginFlowView:
        account_name = self._normalize_account_name(account_name)
        if method not in {"qr", "phone"}:
            raise AdminAccountError("LOGIN_METHOD_INVALID", "登录方式仅支持二维码或手机号")

        api_id, api_hash = self._credentials()
        async with self._lock:
            previous_id = self._flow_ids_by_account.get(account_name)
            previous = self._flows_by_id.get(previous_id) if previous_id else None
            if previous is not None and previous.stage in _ACTIVE_STAGES:
                raise AdminAccountError("LOGIN_FLOW_CONFLICT", "该账号已有登录流程正在进行")
            if previous is not None:
                self._flows_by_id.pop(previous.flow_id, None)
            try:
                client = self._client_factory(
                    str(self.settings.sessions_dir / account_name), api_id, api_hash
                )
            except Exception:
                raise AdminAccountError(
                    "LOGIN_START_FAILED", "无法启动 Telegram 登录流程"
                ) from None
            flow = _LoginFlow(
                flow_id=str(uuid.uuid4()),
                account_name=account_name,
                method=method,
                stage="connecting" if method == "qr" else "phone_required",
                client=client,
            )
            self._flows_by_id[flow.flow_id] = flow
            self._flow_ids_by_account[account_name] = flow.flow_id

        if method == "phone":
            return flow.view()

        try:
            await self._connect(flow)
            if await flow.client.is_user_authorized():
                await self._complete(flow, await flow.client.get_me())
                return flow.view()
            await self._create_qr(flow)
            flow.waiter = asyncio.create_task(
                self._wait_for_qr(flow), name=f"telegram-qr-login:{flow.flow_id}"
            )
            return flow.view()
        except AdminAccountError:
            raise
        except Exception:
            await self._fail_start(flow)
            raise AdminAccountError("LOGIN_START_FAILED", "无法启动 Telegram 登录流程") from None

    async def start_qr(self, account_name: str) -> LoginFlowView:
        return await self.start(account_name, "qr")

    async def start_phone(self, account_name: str, phone: str | None = None) -> LoginFlowView:
        flow = await self.start(account_name, "phone")
        if phone is not None:
            return await self.submit_phone(flow.flow_id, phone)
        return flow

    async def submit_phone(self, flow_id: str, phone: str) -> LoginFlowView:
        flow = await self._require_flow(flow_id)
        async with flow.operation_lock:
            self._require_stage(flow, "phone_required")
            try:
                await self._connect(flow)
                if await flow.client.is_user_authorized():
                    await self._complete(flow, await flow.client.get_me())
                    return flow.view()
                await flow.client.send_code_request(phone)
            except PhoneNumberInvalidError:
                raise AdminAccountError("PHONE_INVALID", "手机号无效") from None
            except FloodWaitError:
                raise AdminAccountError("TELEGRAM_RATE_LIMITED", "请求过于频繁，请稍后重试") from None
            except AdminAccountError:
                raise
            except Exception:
                raise AdminAccountError("PHONE_CODE_SEND_FAILED", "验证码发送失败") from None
            finally:
                # Do not retain another reference to the decrypted input.
                phone = ""
            flow.stage = "code_pending"
            flow.updated_at = utc_now()
            return flow.view()

    async def submit_code(self, flow_id: str, code: str) -> LoginFlowView:
        flow = await self._require_flow(flow_id)
        async with flow.operation_lock:
            self._require_stage(flow, "code_pending")
            try:
                # Telethon remembers the phone/hash from send_code_request, so
                # the manager does not need to retain a phone number itself.
                user = await flow.client.sign_in(code=code)
            except SessionPasswordNeededError:
                flow.stage = "password_pending"
                flow.updated_at = utc_now()
                return flow.view()
            except (PhoneCodeEmptyError, PhoneCodeHashEmptyError, PhoneCodeInvalidError):
                raise AdminAccountError("PHONE_CODE_INVALID", "验证码无效") from None
            except PhoneCodeExpiredError:
                flow.stage = "phone_required"
                flow.updated_at = utc_now()
                raise AdminAccountError("PHONE_CODE_EXPIRED", "验证码已过期，请重新获取") from None
            except FloodWaitError:
                raise AdminAccountError("TELEGRAM_RATE_LIMITED", "请求过于频繁，请稍后重试") from None
            except Exception:
                raise AdminAccountError("PHONE_CODE_VERIFY_FAILED", "验证码校验失败") from None
            finally:
                code = ""
            await self._complete(flow, user)
            return flow.view()

    async def submit_password(self, flow_id: str, password: str) -> LoginFlowView:
        flow = await self._require_flow(flow_id)
        async with flow.operation_lock:
            self._require_stage(flow, "password_pending")
            try:
                user = await flow.client.sign_in(password=password)
            except PasswordHashInvalidError:
                raise AdminAccountError("TWO_FACTOR_INVALID", "二次验证密码无效") from None
            except FloodWaitError:
                raise AdminAccountError("TELEGRAM_RATE_LIMITED", "请求过于频繁，请稍后重试") from None
            except Exception:
                raise AdminAccountError("TWO_FACTOR_VERIFY_FAILED", "二次验证失败") from None
            finally:
                password = ""
            await self._complete(flow, user)
            return flow.view()

    async def get_flow(self, flow_id: str) -> LoginFlowView:
        return (await self._require_flow(flow_id)).view()

    async def cancel(self, flow_id: str) -> None:
        flow = await self._require_flow(flow_id)
        async with self._lock:
            self._flows_by_id.pop(flow.flow_id, None)
            if self._flow_ids_by_account.get(flow.account_name) == flow.flow_id:
                self._flow_ids_by_account.pop(flow.account_name, None)
        waiter = flow.waiter
        if waiter is not None and waiter is not asyncio.current_task() and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await self._disconnect(flow)

    async def close(self) -> None:
        async with self._lock:
            flow_ids = tuple(self._flows_by_id)
        for flow_id in flow_ids:
            try:
                await self.cancel(flow_id)
            except AdminAccountError:
                pass

    def list_accounts(self) -> list[AccountView]:
        with self.database.session() as session:
            accounts = list(session.scalars(select(Account).order_by(Account.created_at, Account.name)))
        tasks = self.database.list_tasks(include_archived=True)
        return [
            self._account_view(
                account,
                task_count=sum(task.account_id == account.id for task in tasks),
                enabled_task_count=sum(
                    task.account_id == account.id and task.enabled for task in tasks
                ),
            )
            for account in accounts
        ]

    def logout_impact(self, account_id_or_name: str) -> LogoutImpact:
        account = self._find_account(account_id_or_name)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        tasks = [
            AccountTaskImpact(
                task_id=task.id,
                name=task.name,
                enabled=task.enabled,
                archived=task.archived,
            )
            for task in self.database.list_tasks(include_archived=True)
            if task.account_id == account.id
        ]
        return LogoutImpact(account.id, account.name, tuple(tasks))

    async def logout(self, account_id_or_name: str) -> LogoutImpact:
        impact = self.logout_impact(account_id_or_name)
        if impact.enabled_task_ids:
            raise AdminAccountError("ACCOUNT_HAS_ENABLED_TASKS", "账号仍有关联的启用任务，无法退出")
        account = self.database.get_account_by_id(impact.account_id)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")

        try:
            await self._disconnect_pooled_client(account)
            api_id, api_hash = self._credentials()
            client = self._client_factory(
                str(self.settings.sessions_dir / account.session_name), api_id, api_hash
            )
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
        except AdminAccountError:
            raise
        except Exception:
            raise AdminAccountError("ACCOUNT_LOGOUT_FAILED", "Telegram 账号退出失败") from None
        finally:
            if "client" in locals():
                try:
                    result = client.disconnect()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass

        with self.database.session() as session:
            stored = session.get(Account, account.id)
            if stored is not None:
                stored.is_active = False
                session.commit()
        return impact

    async def _connect(self, flow: _LoginFlow) -> None:
        if not flow.connected:
            await flow.client.connect()
            flow.connected = True

    async def _create_qr(self, flow: _LoginFlow) -> Any:
        login = await flow.client.qr_login()
        flow.qr_login = login
        flow.qr_url = str(login.url)
        flow.qr_expires_at = self._utc_datetime(getattr(login, "expires", None))
        flow.stage = "qr_pending"
        flow.updated_at = utc_now()
        return login

    async def _wait_for_qr(self, flow: _LoginFlow) -> None:
        try:
            while await self._is_current(flow):
                login = flow.qr_login
                if login is None:
                    login = await self._create_qr(flow)
                try:
                    await login.wait()
                except asyncio.TimeoutError:
                    if not await self._is_current(flow):
                        return
                    await self._create_qr(flow)
                    continue
                except SessionPasswordNeededError:
                    flow.qr_url = None
                    flow.qr_expires_at = None
                    flow.qr_login = None
                    flow.stage = "password_pending"
                    flow.updated_at = utc_now()
                    return
                await self._complete(flow, await flow.client.get_me())
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            if await self._is_current(flow):
                flow.qr_url = None
                flow.qr_expires_at = None
                flow.stage = "failed"
                flow.updated_at = utc_now()
                await self._disconnect(flow)

    async def _complete(self, flow: _LoginFlow, user: Any) -> None:
        phone = getattr(user, "phone", None)
        account = self.database.get_account(flow.account_name)
        if account is None:
            account = self.database.save_account(
                Account(
                    name=flow.account_name,
                    phone=phone,
                    session_name=flow.account_name,
                    is_active=True,
                )
            )
        else:
            with self.database.session() as session:
                stored = session.get(Account, account.id)
                if stored is None:
                    raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
                stored.phone = phone
                stored.is_active = True
                session.commit()
                session.refresh(stored)
                account = stored
        flow.account_id = account.id
        flow.qr_url = None
        flow.qr_expires_at = None
        flow.qr_login = None
        flow.stage = "completed"
        flow.updated_at = utc_now()
        await self._disconnect(flow)

    async def _fail_start(self, flow: _LoginFlow) -> None:
        async with self._lock:
            self._flows_by_id.pop(flow.flow_id, None)
            if self._flow_ids_by_account.get(flow.account_name) == flow.flow_id:
                self._flow_ids_by_account.pop(flow.account_name, None)
        await self._disconnect(flow)

    async def _disconnect(self, flow: _LoginFlow) -> None:
        client = flow.client
        if client is None:
            return
        try:
            result = client.disconnect()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass
        finally:
            flow.connected = False
            flow.client = None

    async def _disconnect_pooled_client(self, account: Account) -> None:
        if self._client_pool is None:
            return
        remover = getattr(self._client_pool, "disconnect_account", None)
        if remover is not None:
            result = remover(account)
            if inspect.isawaitable(result):
                await result
            return
        clients = getattr(self._client_pool, "clients", None)
        if isinstance(clients, dict):
            client = clients.pop(account.id, None)
            if client is not None:
                result = client.disconnect()
                if inspect.isawaitable(result):
                    await result

    async def _require_flow(self, flow_id: str) -> _LoginFlow:
        async with self._lock:
            flow = self._flows_by_id.get(flow_id)
        if flow is None:
            raise AdminAccountError("LOGIN_FLOW_NOT_FOUND", "登录流程不存在或已结束")
        return flow

    async def _is_current(self, flow: _LoginFlow) -> bool:
        async with self._lock:
            return self._flows_by_id.get(flow.flow_id) is flow

    @staticmethod
    def _require_stage(flow: _LoginFlow, expected: LoginStage) -> None:
        if flow.stage != expected:
            raise AdminAccountError("LOGIN_STAGE_INVALID", "当前登录流程阶段不允许此操作")

    @staticmethod
    def _normalize_account_name(account_name: str) -> str:
        normalized = account_name.strip()
        if not _ACCOUNT_NAME.fullmatch(normalized) or normalized in {".", ".."}:
            raise AdminAccountError("ACCOUNT_NAME_INVALID", "账号名称格式无效")
        return normalized

    def _credentials(self) -> tuple[int, str]:
        try:
            return self.settings.require_api_credentials()
        except Exception:
            raise AdminAccountError(
                "TELEGRAM_CONFIGURATION_INVALID", "Telegram API 配置不可用"
            ) from None

    def _find_account(self, account_id_or_name: str) -> Account | None:
        return self.database.get_account_by_id(account_id_or_name) or self.database.get_account(
            account_id_or_name
        )

    @staticmethod
    def _account_view(
        account: Account, *, task_count: int = 0, enabled_task_count: int = 0
    ) -> AccountView:
        return AccountView(
            account_id=account.id,
            name=account.name,
            phone_masked=LoginFlowManager._mask_phone(account.phone),
            session_name=account.session_name,
            is_active=account.is_active,
            created_at=account.created_at,
            task_count=task_count,
            enabled_task_count=enabled_task_count,
        )

    @staticmethod
    def _mask_phone(phone: str | None) -> str | None:
        if not phone:
            return None
        if len(phone) <= 4:
            return "*" * len(phone)
        return f"{phone[:3]}{'*' * (len(phone) - 5)}{phone[-2:]}"

    @staticmethod
    def _utc_datetime(value: Any) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
