from __future__ import annotations

import asyncio
import inspect
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from sqlalchemy import select
from telethon import TelegramClient, events
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
from telethon.tl.types import Channel, Chat, User

from tg_botx.config import Settings
from tg_botx.infrastructure.persistence.db import Account, Database, utc_now
from tg_botx.integrations.telegram import TelegramAccountConfig, create_telethon_client

logger = logging.getLogger(__name__)


def _create_telegram_client(session_path: str, api_id: int, api_hash: str) -> TelegramClient:
    return create_telethon_client(
        TelegramAccountConfig(
            api_id=api_id,
            api_hash=api_hash,
            session_path=Path(session_path),
        )
    )


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
class ChatView:
    chat_id: str
    chat_type: Literal["bot", "group", "private"]
    title: str
    username: str | None
    has_avatar: bool
    avatar_photo_id: int | None = None


@dataclass(frozen=True, slots=True)
class ChatPullView:
    account_id: str
    added: int
    updated: int
    removed: int
    total: int
    synced_at: datetime


@dataclass(frozen=True, slots=True)
class MessageProbeView:
    message_id: int
    text: str
    buttons: tuple[dict[str, Any], ...]


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
        client_factory: Callable[[str, int, str], Any] = _create_telegram_client,
        client_pool: Any | None = None,
    ):
        self.settings = settings
        self.database = database
        self._client_factory = client_factory
        self._client_pool = client_pool
        self._flows_by_id: dict[str, _LoginFlow] = {}
        self._flow_ids_by_account: dict[str, str] = {}
        self._avatar_prefetch_tasks: set[asyncio.Task[None]] = set()
        self._avatar_download_locks: dict[str, asyncio.Lock] = {}
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
        prefetch_tasks = tuple(self._avatar_prefetch_tasks)
        for task in prefetch_tasks:
            task.cancel()
        if prefetch_tasks:
            await asyncio.gather(*prefetch_tasks, return_exceptions=True)
        async with self._lock:
            flow_ids = tuple(self._flows_by_id)
        for flow_id in flow_ids:
            try:
                await self.cancel(flow_id)
            except AdminAccountError:
                pass

    def list_accounts(self) -> list[AccountView]:
        with self.database.session() as session:
            accounts = list(
                session.scalars(select(Account).order_by(Account.created_at, Account.name))
            )
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

    async def list_chats(
        self,
        account_id_or_name: str,
        *,
        chat_type: Literal["all", "bot", "group", "private"] = "all",
        query: str | None = None,
        limit: int = 200,
    ) -> list[ChatView]:
        account = self._find_account(account_id_or_name)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        if not account.is_active:
            raise AdminAccountError("ACCOUNT_INACTIVE", "Telegram 账号已停用")
        if chat_type not in {"all", "bot", "group", "private"}:
            raise AdminAccountError("CHAT_TYPE_INVALID", "聊天类型无效")
        try:
            rows = self.database.list_account_chats(
                account.id,
                chat_type=chat_type,
                query=query,
                limit=limit,
            )
        except Exception:
            raise AdminAccountError("CHAT_LIST_FAILED", "无法加载账号对话") from None
        return [
            ChatView(
                chat_id=row.chat_id,
                chat_type=row.chat_type,  # type: ignore[arg-type]
                title=row.title,
                username=row.username,
                has_avatar=row.has_avatar,
                avatar_photo_id=row.avatar_photo_id,
            )
            for row in rows
        ]

    async def pull_chats(
        self,
        account_id_or_name: str,
        *,
        client: Any | None = None,
    ) -> ChatPullView:
        """Pull all dialogs from Telegram and incrementally cache them."""

        account = self._find_account(account_id_or_name)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        if not account.is_active:
            raise AdminAccountError("ACCOUNT_INACTIVE", "Telegram 账号已停用")
        pooled_lease = client is None
        telegram_client = client or await self._acquire_pooled_client(account)
        chats: list[dict[str, Any]] = []
        avatar_jobs: list[tuple[str, Any, int]] = []
        try:
            async for dialog in telegram_client.iter_dialogs():
                entity = dialog.entity
                kind = self._chat_type(entity)
                if kind is None:
                    continue
                avatar_photo_id = self._chat_photo_id(entity)
                username = getattr(entity, "username", None)
                username_value = f"@{username}" if username else None
                chats.append(
                    {
                        "chat_id": str(entity.id),
                        "chat_type": kind,
                        "title": self._chat_title(entity, dialog),
                        "username": username_value,
                        "has_avatar": avatar_photo_id is not None,
                        "avatar_photo_id": avatar_photo_id,
                    }
                )
                if avatar_photo_id is not None:
                    avatar_jobs.append((str(entity.id), entity, avatar_photo_id))
        except asyncio.CancelledError:
            if pooled_lease:
                await self._release_pooled_client(account)
            raise
        except Exception:
            if pooled_lease:
                await self._release_pooled_client(account)
            raise AdminAccountError("CHAT_PULL_FAILED", "无法拉取账号对话") from None

        try:
            result = self.database.upsert_account_chats(account.id, chats)
        except asyncio.CancelledError:
            if pooled_lease:
                await self._release_pooled_client(account)
            raise
        except Exception:
            if pooled_lease:
                await self._release_pooled_client(account)
            raise AdminAccountError("CHAT_PULL_FAILED", "无法保存账号对话") from None
        # The API should return as soon as the dialog snapshot is persisted.
        # Avatar downloads are independent and run in the background.  Only a
        # pooled client is safe to use after this method returns; login-flow
        # clients are disconnected immediately after the initial pull.
        if avatar_jobs and pooled_lease:
            # Transfer the lease to the background downloader; otherwise the
            # client would be disconnected while avatar requests are running.
            self._schedule_avatar_prefetch(
                account.id, telegram_client, avatar_jobs, account=account
            )
            pooled_lease = False
        if pooled_lease:
            await self._release_pooled_client(account)
        return ChatPullView(
            account_id=account.id,
            added=result["added"],
            updated=result["updated"],
            removed=result["removed"],
            total=result["total"],
            synced_at=utc_now(),
        )

    async def download_chat_avatar(self, account_id_or_name: str, chat_id: str) -> Path | None:
        """Return a locally cached avatar without contacting Telegram.

        Avatar files are populated asynchronously while chats are pulled.  A
        request for an avatar must remain a cheap, read-only cache lookup: a
        missing file is represented by ``None`` and the API turns that into a
        404 response.
        """

        account = self._find_account(account_id_or_name)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        if not account.is_active:
            raise AdminAccountError("ACCOUNT_INACTIVE", "Telegram 账号已停用")
        if not re.fullmatch(r"-?\d+", chat_id):
            raise AdminAccountError("CHAT_ID_INVALID", "聊天 ID 无效")

        cache_dir = self._avatar_cache_dir()
        try:
            get_account_chat = getattr(self.database, "get_account_chat", None)
            chat = get_account_chat(account.id, chat_id) if get_account_chat else None
        except AdminAccountError:
            raise
        except Exception:
            raise AdminAccountError("CHAT_AVATAR_FAILED", "无法读取聊天头像缓存") from None

        photo_id = getattr(chat, "avatar_photo_id", None) if chat is not None else None
        if photo_id is not None:
            cache_path = cache_dir / f"{chat_id}-{photo_id}.jpg"
            if self._valid_avatar_file(cache_path):
                return cache_path
            # A known photo version with no matching file is a cache miss.
            # Do not serve an older version under the same chat id.
            return None

        # Keep avatars cached by older versions (or rows created before the
        # photo id column existed) usable.  A row explicitly marked as having
        # no avatar must not resurrect a stale file.  This still performs no
        # network or database mutation and simply returns None when no file is
        # present.
        if chat is not None and not getattr(chat, "has_avatar", False):
            return None
        return self._find_legacy_avatar(cache_dir, chat_id)

    def _schedule_avatar_prefetch(
        self,
        account_id: str,
        client: Any,
        jobs: list[tuple[str, Any, int]],
        *,
        account: Account | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._prefetch_chat_avatars(account_id, client, jobs, account=account)
        )
        self._avatar_prefetch_tasks.add(task)

        def on_done(completed: asyncio.Task[None]) -> None:
            self._avatar_prefetch_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception:
                logger.exception("后台预下载聊天头像失败 account_id=%s", account_id)

        task.add_done_callback(on_done)

    async def _prefetch_chat_avatars(
        self,
        account_id: str,
        client: Any,
        jobs: list[tuple[str, Any, int]],
        *,
        account: Account | None = None,
    ) -> None:
        semaphore = asyncio.Semaphore(4)

        async def download(job: tuple[str, Any, int]) -> None:
            chat_id, entity, photo_id = job
            async with semaphore:
                try:
                    await self._download_avatar_file(client, entity, chat_id, photo_id)
                except Exception:
                    logger.warning(
                        "后台下载聊天头像失败 account_id=%s chat_id=%s",
                        account_id,
                        chat_id,
                        exc_info=True,
                    )

        try:
            await asyncio.gather(*(download(job) for job in jobs))
        finally:
            if account is not None:
                await self._release_pooled_client(account)

    async def _download_avatar_file(
        self,
        client: Any,
        entity: Any,
        chat_id: str,
        photo_id: int,
    ) -> Path | None:
        cache_dir = self._avatar_cache_dir()
        cache_path = cache_dir / f"{chat_id}-{photo_id}.jpg"
        lock = self._avatar_download_locks.setdefault(str(cache_path), asyncio.Lock())
        async with lock:
            # A pull prefetch and a browser request can arrive at the same
            # time. Re-check after acquiring the lock to avoid duplicate
            # Telegram downloads for the same avatar.
            if self._valid_avatar_file(cache_path):
                return cache_path

            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary_path = cache_dir / f".{cache_path.name}.{uuid.uuid4().hex}.tmp"
            try:
                downloaded = await client.download_profile_photo(entity, file=str(temporary_path))
                if downloaded is None or not temporary_path.is_file():
                    return None
                if temporary_path.stat().st_size <= 0:
                    return None
                temporary_path.replace(cache_path)
            finally:
                temporary_path.unlink(missing_ok=True)

            # Keep only the current photo for this chat.  Old versions are
            # never needed after the photo id has changed.
            for stale_path in cache_dir.glob(f"{chat_id}-*.jpg"):
                if stale_path != cache_path:
                    stale_path.unlink(missing_ok=True)
        return cache_path

    def _avatar_cache_dir(self) -> Path:
        return self.settings.data_dir / "cache" / "avatars"

    @staticmethod
    def _valid_avatar_file(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _find_legacy_avatar(self, cache_dir: Path, chat_id: str) -> Path | None:
        try:
            candidates = [
                path for path in cache_dir.glob(f"{chat_id}-*.jpg") if self._valid_avatar_file(path)
            ]
        except OSError:
            return None
        if not candidates:
            return None
        # There should normally be one file.  Choosing the newest makes the
        # fallback deterministic if an interrupted previous download left
        # multiple versions behind.
        try:
            return max(candidates, key=lambda path: path.stat().st_mtime_ns)
        except OSError:
            return None

    async def probe_message(
        self,
        account_id_or_name: str,
        target: str,
        text: str,
        *,
        timeout_seconds: int = 30,
    ) -> MessageProbeView:
        """Send a probe command and return the next bot message's buttons."""

        account = self._find_account(account_id_or_name)
        if account is None:
            raise AdminAccountError("ACCOUNT_NOT_FOUND", "Telegram 账号不存在")
        if not account.is_active:
            raise AdminAccountError("ACCOUNT_INACTIVE", "Telegram 账号已停用")
        if not target.strip():
            raise AdminAccountError("CHAT_TARGET_INVALID", "目标聊天不能为空")
        if not text:
            raise AdminAccountError("MESSAGE_TEXT_INVALID", "发送内容不能为空")
        if timeout_seconds < 1 or timeout_seconds > 120:
            raise AdminAccountError("MESSAGE_TIMEOUT_INVALID", "等待时间必须在 1–120 秒之间")

        client = await self._acquire_pooled_client(account)
        try:
            entity = await client.get_entity(target.strip())
            sent = await client.send_message(entity, text)
            baseline = int(getattr(sent, "id", 0) or 0)
            loop = asyncio.get_running_loop()
            future: asyncio.Future[Any] = loop.create_future()

            async def inspect(message: Any) -> None:
                if future.done() or int(getattr(message, "id", 0) or 0) <= baseline:
                    return
                target_id = getattr(entity, "id", None)
                sender_id = getattr(message, "sender_id", None)
                if (
                    target_id is not None
                    and getattr(entity, "bot", False)
                    and sender_id != target_id
                ):
                    return
                future.set_result(message)

            async def handler(event: Any) -> None:
                await inspect(event.message)

            client.add_event_handler(handler, events.NewMessage(chats=entity))
            try:
                messages = await client.get_messages(entity, limit=20, min_id=baseline)
                for message in reversed(messages or []):
                    await inspect(message)
                    if future.done():
                        break
                try:
                    received = await asyncio.wait_for(future, timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    raise AdminAccountError("MESSAGE_WAIT_TIMEOUT", "等待机器人回复超时") from None
            finally:
                client.remove_event_handler(handler, events.NewMessage(chats=entity))
        except AdminAccountError:
            await self._release_pooled_client(account)
            raise
        except asyncio.CancelledError:
            await self._release_pooled_client(account)
            raise
        except Exception:
            await self._release_pooled_client(account)
            raise AdminAccountError("MESSAGE_PROBE_FAILED", "发送指令或读取回复失败") from None

        try:
            refreshed = await client.get_messages(entity, ids=received.id)
            if isinstance(refreshed, (list, tuple)):
                refreshed = refreshed[0] if refreshed else None
            if refreshed is not None:
                received = refreshed
        except asyncio.CancelledError:
            await self._release_pooled_client(account)
            raise
        except Exception:
            pass
        await self._release_pooled_client(account)

        buttons = getattr(received, "buttons", None) or []
        if not buttons:
            markup_rows = getattr(getattr(received, "reply_markup", None), "rows", None) or []
            buttons = [getattr(row, "buttons", None) or [] for row in markup_rows]
        rows: list[dict[str, Any]] = []
        for row_index, row in enumerate(buttons):
            for column_index, button in enumerate(row):
                callback_data = getattr(button, "data", None)
                if isinstance(callback_data, bytes):
                    callback_value = callback_data.decode("utf-8", errors="replace")
                elif callback_data is None:
                    callback_value = None
                else:
                    callback_value = str(callback_data)
                rows.append(
                    {
                        "row": row_index,
                        "column": column_index,
                        "text": str(getattr(button, "text", "") or ""),
                        "callbackData": callback_value,
                    }
                )
        return MessageProbeView(
            message_id=int(getattr(received, "id", 0) or 0),
            text=str(getattr(received, "raw_text", "") or ""),
            buttons=tuple(rows),
        )

    async def _get_pooled_client(self, account: Account) -> Any:
        if self._client_pool is None:
            raise AdminAccountError("TELEGRAM_UNAVAILABLE", "Telegram 服务暂不可用")
        try:
            return await self._client_pool.get(account)
        except Exception:
            raise AdminAccountError("TELEGRAM_UNAVAILABLE", "无法连接 Telegram 账号") from None

    async def _acquire_pooled_client(self, account: Account) -> Any:
        if self._client_pool is None:
            raise AdminAccountError("TELEGRAM_UNAVAILABLE", "Telegram 服务暂不可用")
        try:
            acquire = getattr(self._client_pool, "acquire", None)
            return await (
                acquire(account) if acquire is not None else self._client_pool.get(account)
            )
        except Exception:
            raise AdminAccountError("TELEGRAM_UNAVAILABLE", "无法连接 Telegram 账号") from None

    async def _release_pooled_client(self, account: Account) -> None:
        if self._client_pool is None:
            return
        release = getattr(self._client_pool, "release", None)
        if release is None:
            return
        try:
            await release(account)
        except Exception:
            logger.warning("释放 Telegram 账号连接失败 account_id=%s", account.id, exc_info=True)

    @staticmethod
    def _chat_type(entity: Any) -> Literal["bot", "group", "private"] | None:
        if isinstance(entity, User):
            return "bot" if bool(getattr(entity, "bot", False)) else "private"
        if isinstance(entity, (Chat, Channel)):
            return "group"
        return None

    @staticmethod
    def _chat_photo_id(entity: Any) -> int | None:
        photo = getattr(entity, "photo", None)
        photo_id = getattr(photo, "photo_id", None)
        return photo_id if isinstance(photo_id, int) else None

    @staticmethod
    def _chat_title(entity: Any, dialog: Any) -> str:
        if isinstance(entity, User):
            name = " ".join(
                value
                for value in (
                    getattr(entity, "first_name", None),
                    getattr(entity, "last_name", None),
                )
                if value
            ).strip()
            return name or getattr(entity, "username", None) or str(entity.id)
        return getattr(dialog, "title", None) or getattr(entity, "title", None) or str(entity.id)

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
        self._ensure_pooled_client_idle(account)

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
        is_new_account = account is None
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
        if self._client_pool is None:
            return
        remover = getattr(self._client_pool, "disconnect_account", None)
        if remover is not None:
            result = remover(account)
            if inspect.isawaitable(result):
                result = await result
            if result is False:
                raise AdminAccountError("ACCOUNT_BUSY", "账号当前正在执行任务，请稍后再试")
            return
        clients = getattr(self._client_pool, "clients", None)
        if isinstance(clients, dict):
            client = clients.pop(account.id, None)
            if client is not None:
                result = client.disconnect()
                if inspect.isawaitable(result):
                    await result

    def _ensure_pooled_client_idle(self, account: Account) -> None:
        if self._client_pool is None:
            return
        checker = getattr(self._client_pool, "has_active_leases", None)
        if checker is not None and checker(account):
            raise AdminAccountError("ACCOUNT_BUSY", "账号当前正在执行任务，请稍后再试")

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
