from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

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
from tg_botx.features.accounts.access import AccountAccess
from tg_botx.features.accounts.avatars import AvatarCache
from tg_botx.features.accounts.chats import ChatDirectory
from tg_botx.features.accounts.directory import AccountDirectory
from tg_botx.features.accounts.models import (
    _ACCOUNT_NAME,
    _ACTIVE_STAGES,
    AccountView,
    AdminAccountError,
    ChatPullView,
    ChatView,
    LoginFlowView,
    LoginMethod,
    LoginStage,
    LogoutImpact,
    MessageProbeView,
    _create_telegram_client,
    _LoginFlow,
)
from tg_botx.features.accounts.probe import MessageProbe
from tg_botx.infrastructure.persistence.db import Account, Database, utc_now

logger = logging.getLogger(__name__)


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
        client_factory: Callable[[str, int, str], Any] = _create_telegram_client,
        client_pool: Any | None = None,
    ):
        self.settings = settings
        self.database = database
        self._client_factory = client_factory
        self._client_pool = client_pool
        self._flows_by_id: dict[str, _LoginFlow] = {}
        self._flow_ids_by_account: dict[str, str] = {}
        self.access = AccountAccess(settings, database, client_factory, client_pool)
        self.avatars = AvatarCache(settings, database, self.access)
        self.chats = ChatDirectory(database, self.access, self.avatars)
        self.probe = MessageProbe(self.access)
        self.directory = AccountDirectory(settings, database, self.access, client_factory)
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
            existing_account = self.database.get_account(account_name)
            if existing_account is not None:
                self._ensure_pooled_client_idle(existing_account)
                await self._disconnect_pooled_client(existing_account)
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
                raise AdminAccountError(
                    "TELEGRAM_RATE_LIMITED", "请求过于频繁，请稍后重试"
                ) from None
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
                raise AdminAccountError(
                    "TELEGRAM_RATE_LIMITED", "请求过于频繁，请稍后重试"
                ) from None
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
                raise AdminAccountError(
                    "TELEGRAM_RATE_LIMITED", "请求过于频繁，请稍后重试"
                ) from None
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
            with contextlib.suppress(AdminAccountError):
                await self.cancel(flow_id)
        await self.avatars.close()

    def list_accounts(self) -> list[AccountView]:
        return self.directory.list_accounts()

    async def list_chats(
        self,
        account_id_or_name: str,
        *,
        chat_type: Literal["all", "bot", "group", "private"] = "all",
        query: str | None = None,
        limit: int = 200,
    ) -> list[ChatView]:
        return await self.chats.list_chats(
            account_id_or_name, chat_type=chat_type, query=query, limit=limit
        )

    async def pull_chats(
        self, account_id_or_name: str, *, client: Any | None = None
    ) -> ChatPullView:
        return await self.chats.pull_chats(account_id_or_name, client=client)

    async def download_chat_avatar(self, account_id_or_name: str, chat_id: str) -> Path | None:
        return await self.avatars.download_chat_avatar(account_id_or_name, chat_id)

    def _schedule_avatar_prefetch(
        self,
        account_id: str,
        client: Any,
        jobs: list[tuple[str, Any, int]],
        *,
        account: Account | None = None,
    ) -> None:
        return self.avatars._schedule_avatar_prefetch(account_id, client, jobs, account=account)

    async def _prefetch_chat_avatars(
        self,
        account_id: str,
        client: Any,
        jobs: list[tuple[str, Any, int]],
        *,
        account: Account | None = None,
    ) -> None:
        return await self.avatars._prefetch_chat_avatars(account_id, client, jobs, account=account)

    async def _download_avatar_file(
        self, client: Any, entity: Any, chat_id: str, photo_id: int
    ) -> Path | None:
        return await self.avatars._download_avatar_file(client, entity, chat_id, photo_id)

    def _avatar_cache_dir(self) -> Path:
        return self.avatars._avatar_cache_dir()

    @staticmethod
    def _valid_avatar_file(path: Path) -> bool:
        return AvatarCache._valid_avatar_file(path)

    def _find_legacy_avatar(self, cache_dir: Path, chat_id: str) -> Path | None:
        return self.avatars._find_legacy_avatar(cache_dir, chat_id)

    async def probe_message(
        self, account_id_or_name: str, target: str, text: str, *, timeout_seconds: int = 30
    ) -> MessageProbeView:
        return await self.probe.probe_message(
            account_id_or_name, target, text, timeout_seconds=timeout_seconds
        )

    async def _get_pooled_client(self, account: Account) -> Any:
        return await self.access._get_pooled_client(account)

    async def _acquire_pooled_client(self, account: Account) -> Any:
        return await self.access._acquire_pooled_client(account)

    async def _release_pooled_client(self, account: Account) -> None:
        return await self.access._release_pooled_client(account)

    @staticmethod
    def _chat_type(entity: Any) -> Literal["bot", "group", "private"] | None:
        return ChatDirectory._chat_type(entity)

    @staticmethod
    def _chat_photo_id(entity: Any) -> int | None:
        return ChatDirectory._chat_photo_id(entity)

    @staticmethod
    def _chat_title(entity: Any, dialog: Any) -> str:
        return ChatDirectory._chat_title(entity, dialog)

    def logout_impact(self, account_id_or_name: str) -> LogoutImpact:
        return self.directory.logout_impact(account_id_or_name)

    async def logout(self, account_id_or_name: str) -> LogoutImpact:
        return await self.directory.logout(account_id_or_name)

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
                except TimeoutError:
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
        is_new_account = account is None
        account = self.database.activate_account(flow.account_name, phone)
        flow.account_id = account.id
        if is_new_account and flow.client is not None:
            try:
                await self.pull_chats(account.id, client=flow.client)
            except AdminAccountError as exc:
                # Login has completed successfully; an unavailable Telegram
                # dialog snapshot should not invalidate the new account.
                logger.warning("账号首次同步聊天失败 account_id=%s code=%s", account.id, exc.code)
            except Exception as exc:
                logger.warning(
                    "账号首次同步聊天发生未预期异常 account_id=%s type=%s",
                    account.id,
                    type(exc).__name__,
                )
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
        return await self.access._disconnect_pooled_client(account)

    def _ensure_pooled_client_idle(self, account: Account) -> None:
        return self.access._ensure_pooled_client_idle(account)

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
        return self.access._credentials()

    def _find_account(self, account_id_or_name: str) -> Account | None:
        return self.access._find_account(account_id_or_name)

    @staticmethod
    def _account_view(
        account: Account, *, task_count: int = 0, enabled_task_count: int = 0
    ) -> AccountView:
        return AccountDirectory._account_view(
            account, task_count=task_count, enabled_task_count=enabled_task_count
        )

    @staticmethod
    def _mask_phone(phone: str | None) -> str | None:
        return AccountDirectory._mask_phone(phone)

    @staticmethod
    def _utc_datetime(value: Any) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
