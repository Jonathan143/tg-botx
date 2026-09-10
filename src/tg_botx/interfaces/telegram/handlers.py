from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from tg_botx.core.time import format_local_time
from tg_botx.features.bot.executors import (
    CustomCommandContext,
    CustomCommandExecutorError,
    execute_custom_command,
)
from tg_botx.features.bot.management import BotManagementService
from tg_botx.features.bot.models import (
    _CONFIRM_TTL_SECONDS,
    _PAGE_SIZE,
    BotBindingError,
)
from tg_botx.features.checkin.runtime import (
    ManualRunConflict,
    TaskNotFound,
    TaskStateError,
    WorkflowVersionNotFound,
)
from tg_botx.infrastructure.persistence.db import (
    utc_now,
)
from tg_botx.integrations.telegram_bot import TelegramBotApiError

logger = logging.getLogger(__name__)


class BotMessageHandlers:
    def __init__(self, database, checkin, management, client, status):
        self.database = database
        self.checkin = checkin
        self.management = management
        self.client = client
        self.status = status

    def command_configs(self) -> list[dict[str, Any]]:
        return self.management.command_configs()

    async def _handle_update(self, update: dict[str, object]) -> None:
        message = update.get("message")
        if isinstance(message, dict):
            await self._handle_message(message, update.get("update_id"))
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_callback(callback, update.get("update_id"))

    async def _handle_message(self, message: dict[str, object], update_id: object) -> None:
        chat = message.get("chat")
        user = message.get("from")
        text = message.get("text")
        if (
            not isinstance(chat, dict)
            or chat.get("type") != "private"
            or not isinstance(user, dict)
        ):
            return
        user_id, chat_id = self._ids(user, chat)
        if user_id is None or chat_id is None or not isinstance(text, str):
            return
        parsed = text.strip().split(maxsplit=1)
        if not parsed or not parsed[0].startswith("/"):
            return
        command = parsed[0][1:].split("@", 1)[0].casefold()
        canonical_command = "tasks" if command == "task" else command
        argument = parsed[1].strip() if len(parsed) > 1 else ""
        update_number = update_id if isinstance(update_id, int) else None
        config = next(
            (item for item in self.command_configs() if item["command"] == canonical_command),
            None,
        )
        if config is None:
            await self._send(chat_id, "无法识别该命令，请发送 /help 查看可用命令。")
            return
        if not config["enabled"]:
            await self._send(chat_id, "该命令当前已停用，请联系管理员。")
            return
        if not self._command_allowed(user_id, chat_id, canonical_command, update_number):
            await self._send(chat_id, "你没有权限调用该命令。")
            return
        if config.get("type") == "custom":
            await self._custom_command(
                chat_id,
                user_id,
                canonical_command,
                argument,
                text,
                config,
                update_number,
            )
            return
        if command == "start":
            await self._send(chat_id, self._welcome(user_id, chat_id))
        elif command == "help":
            await self._send(chat_id, self._help(user_id, chat_id))
        elif command == "bind":
            await self._bind(chat_id, user_id, user, argument, update_number)
        elif command == "unbind":
            if self.management.unbind(user_id, chat_id):
                await self._send(chat_id, "✅ 已解除管理 Bot 绑定。")
            else:
                await self._send(chat_id, "当前没有可解除的绑定。")
        elif canonical_command == "tasks":
            await self._send_tasks(chat_id, 1)
        elif command == "status":
            await self._send(chat_id, self._system_status())
        elif command == "checkin":
            status, amount, total = self.management.checkin(user_id, chat_id)
            if status == "success":
                self.management.audit(
                    user_id, chat_id, "checkin", "success", update_id=update_number
                )
                await self._send(chat_id, f"✅ 签到成功，获得 {amount} 积分！\n当前积分：{total}")
            elif status == "already":
                await self._send(chat_id, f"你今天已经签到过了。\n当前积分：{total}")
            else:
                self.management.audit(
                    user_id, chat_id, "checkin", "denied", update_id=update_number
                )
                await self._send(chat_id, "请先绑定用户后再签到。")
        else:
            await self._send(chat_id, "无法识别该命令，请发送 /help 查看可用命令。")

    async def _custom_command(
        self,
        chat_id: int,
        user_id: int,
        command: str,
        argument: str,
        text: str,
        config: dict[str, Any],
        update_id: int | None,
    ) -> None:
        try:
            result = await execute_custom_command(
                config.get("executorType", "none"),
                config.get("executorConfig", {}),
                CustomCommandContext(command, argument, text, user_id, chat_id),
            )
        except CustomCommandExecutorError as exc:
            self.management.audit(
                user_id,
                chat_id,
                command,
                "failed",
                update_id=update_id,
                details=str(exc),
            )
            await self._send(chat_id, f"❌ 自定义指令执行失败：{html.escape(str(exc))}")
            return
        except Exception:
            logger.exception("自定义指令执行异常 command=%s", command)
            self.management.audit(
                user_id,
                chat_id,
                command,
                "failed",
                update_id=update_id,
                details="executor internal error",
            )
            await self._send(chat_id, "❌ 自定义指令执行失败，请联系管理员。")
            return
        self.management.audit(user_id, chat_id, command, "success", update_id=update_id)
        if result:
            # The Bot API client uses HTML parse mode. Escaping executor output
            # prevents a remote response or script from injecting markup.
            await self._send(chat_id, html.escape(result))
        else:
            await self._send(chat_id, "✅ 指令执行成功，但没有返回内容。")

    async def _bind(
        self, chat_id: int, user_id: int, user: dict[str, object], code: str, update_id: int | None
    ) -> None:
        if not code:
            await self._send(chat_id, "用法：<code>/bind ABCD-EFGH-IJKL</code>")
            return
        try:
            self.management.bind(code, user_id=user_id, chat_id=chat_id, user=user)
        except BotBindingError as exc:
            self.management.audit(
                user_id, chat_id, "bind", "failed", update_id=update_id, details=str(exc)
            )
            await self._send(chat_id, f"❌ {html.escape(str(exc))}")
            return
        await self._send(chat_id, "✅ 绑定成功。现在可以使用 /tasks、/status 和 /checkin。")

    async def _handle_callback(self, callback: dict[str, object], update_id: object) -> None:
        callback_id = callback.get("id")
        user = callback.get("from")
        data = callback.get("data")
        message = callback.get("message")
        if (
            not isinstance(callback_id, str)
            or not isinstance(user, dict)
            or not isinstance(data, str)
        ):
            return
        try:
            if not isinstance(message, dict):
                return
            chat = message.get("chat")
            message_id = message.get("message_id")
            if (
                not isinstance(chat, dict)
                or chat.get("type") != "private"
                or not isinstance(message_id, int)
            ):
                return
            user_id, chat_id = self._ids(user, chat)
            if user_id is None or chat_id is None:
                return
            update_number = update_id if isinstance(update_id, int) else None
            parts = data.split(":")
            command = (
                "tasks" if parts and parts[0] in {"tasks", "task", "back", "ask", "do"} else None
            )
            if command is None or not self._command_allowed(
                user_id, chat_id, command, update_number
            ):
                if self.client:
                    await self.client.answer_callback(callback_id, "你没有权限调用该命令")
                return
            if self.client:
                await self.client.answer_callback(callback_id)
            if len(parts) == 2 and parts[0] == "tasks":
                await self._edit_tasks(chat_id, message_id, self._page(parts[1]))
            elif len(parts) == 2 and parts[0] == "task":
                await self._edit_task(chat_id, message_id, parts[1])
            elif len(parts) == 3 and parts[0] == "back" and parts[1] == "tasks":
                await self._edit_tasks(chat_id, message_id, self._page(parts[2]))
            elif len(parts) == 4 and parts[0] == "ask":
                await self._edit_confirmation(chat_id, message_id, parts[1], parts[2], parts[3])
            elif len(parts) == 4 and parts[0] == "do":
                await self._perform_action(
                    user_id, chat_id, message_id, parts[1], parts[2], parts[3], update_number
                )
        except TelegramBotApiError as exc:
            logger.warning("管理 Bot 回调响应失败 type=%s", type(exc).__name__)

    def _authorized(self, user_id: int, chat_id: int, update_id: int | None, action: str) -> bool:
        allowed = self.management.is_bound(user_id, chat_id)
        if not allowed:
            self.management.audit(user_id, chat_id, action, "denied", update_id=update_id)
        return allowed

    def _command_allowed(
        self, user_id: int, chat_id: int, command: str, update_id: int | None
    ) -> bool:
        role = self.management.binding_role(user_id, chat_id) or "anonymous"
        config = next((item for item in self.command_configs() if item["command"] == command), None)
        allowed = bool(config and config.get("enabled") and role in config.get("allowedRoles", []))
        if not allowed:
            self.management.audit(user_id, chat_id, command, "denied", update_id=update_id)
        return allowed

    async def _send_tasks(self, chat_id: int, page: int) -> None:
        await self._send(chat_id, *self._task_page(page))

    async def _edit_tasks(self, chat_id: int, message_id: int, page: int) -> None:
        text, markup = self._task_page(page)
        if self.client:
            await self.client.edit_message(chat_id, message_id, text, markup)

    def _task_page(self, page: int) -> tuple[str, dict[str, object]]:
        _, total = self.database.list_tasks_page(page=1, page_size=_PAGE_SIZE)
        pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = min(max(page, 1), pages)
        items, _ = self.database.list_tasks_page(page=page, page_size=_PAGE_SIZE)
        lines = [f"<b>任务列表</b>（第 {page}/{pages} 页，共 {total} 个）"]
        buttons: list[list[dict[str, str]]] = []
        for task in items:
            state = "🟢" if task.enabled else "⚪"
            if task.archived:
                state = "📦"
            lines.append(f"{state} {html.escape(task.name)}")
            buttons.append(
                [{"text": f"{state} {task.name[:45]}", "callback_data": f"task:{task.id}"}]
            )
        if not items:
            lines.append("暂无任务。")
        navigation: list[dict[str, str]] = []
        if page > 1:
            navigation.append({"text": "‹ 上一页", "callback_data": f"tasks:{page - 1}"})
        if page < pages:
            navigation.append({"text": "下一页 ›", "callback_data": f"tasks:{page + 1}"})
        if navigation:
            buttons.append(navigation)
        return "\n".join(lines), {"inline_keyboard": buttons}

    async def _edit_task(self, chat_id: int, message_id: int, task_id: str) -> None:
        if self.client:
            await self.client.edit_message(chat_id, message_id, *self._task_detail(task_id))

    def _task_detail(self, task_id: str) -> tuple[str, dict[str, object]]:
        task = self.database.get_task_any(task_id)
        if task is None:
            return "❌ 任务不存在或已被删除。", {"inline_keyboard": []}
        account = self.database.get_account_by_id(task.account_id)
        last_run = self.database.task_history(task.id, limit=1)
        version = self.database.get_latest_workflow_version(task.id)
        status = "归档" if task.archived else "启用" if task.enabled else "停用"
        run_status = last_run[0].status if last_run else task.last_status or "暂无"
        lines = [
            f"<b>{html.escape(task.name)}</b>",
            f"账号：{html.escape(account.name if account else '未知')}",
            f"目标：{html.escape(task.target)}",
            f"状态：{status}",
            f"下次执行：{self._format_time(task.next_run_at, task.timezone)}",
            f"上次结果：{html.escape(run_status)}",
            f"当前运行：{'是' if self.checkin.running.get(task.id) or self.database.has_running_run(task.id) else '否'}",
            f"发布版本：{version.version_number if version else '未发布'}",
        ]
        buttons: list[list[dict[str, str]]] = []
        if not task.archived:
            action = "disable" if task.enabled else "enable"
            label = "停用任务" if task.enabled else "启用任务"
            buttons.append([{"text": label, "callback_data": f"ask:{action}:{task.id}:0"}])
            buttons.append([{"text": "执行任务", "callback_data": f"ask:run:{task.id}:0"}])
        buttons.append([{"text": "‹ 返回任务列表", "callback_data": "back:tasks:1"}])
        return "\n".join(lines), {"inline_keyboard": buttons}

    async def _edit_confirmation(
        self, chat_id: int, message_id: int, action: str, task_id: str, _: str
    ) -> None:
        task = self.database.get_task_any(task_id)
        if task is None:
            text = "❌ 任务不存在。"
            markup: dict[str, object] = {"inline_keyboard": []}
        else:
            labels = {"enable": "启用", "disable": "停用", "run": "执行"}
            expires = int(time.time()) + _CONFIRM_TTL_SECONDS
            text = f"确认{labels.get(action, '操作')}任务 <b>{html.escape(task.name)}</b>？\n确认按钮将在 60 秒后失效。"
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "确认", "callback_data": f"do:{action}:{task_id}:{expires}"},
                        {"text": "取消", "callback_data": f"task:{task_id}"},
                    ]
                ]
            }
        if self.client:
            await self.client.edit_message(chat_id, message_id, text, markup)

    async def _perform_action(
        self,
        user_id: int,
        chat_id: int,
        message_id: int,
        action: str,
        task_id: str,
        expires: str,
        update_id: int | None,
    ) -> None:
        try:
            if not self.management.is_admin(user_id, chat_id):
                raise BotBindingError("需要管理员权限才能执行此操作")
            if int(expires) < int(time.time()):
                raise BotBindingError("确认按钮已过期，请重新点击操作")
            task = self.database.get_task_any(task_id)
            if task is None:
                raise TaskNotFound("任务不存在")
            if action == "enable":
                task = self.checkin.enable_task(task_id)
                result = "success"
            elif action == "disable":
                task = self.checkin.disable_task(task_id)
                result = "success"
            elif action == "run":
                self.checkin.start_manual_run(task_id)
                result = "success"
            else:
                raise BotBindingError("未知操作")
            self.management.audit(user_id, chat_id, action, result, task=task, update_id=update_id)
            prefix = {
                "enable": "✅ 任务已启用",
                "disable": "✅ 任务已停用",
                "run": "✅ 任务已开始执行",
            }[action]
            text, markup = self._task_detail(task_id)
            text = f"{prefix}\n\n{text}"
        except (
            ValueError,
            BotBindingError,
            TaskNotFound,
            TaskStateError,
            ManualRunConflict,
            WorkflowVersionNotFound,
        ) as exc:
            task = self.database.get_task_any(task_id)
            self.management.audit(
                user_id, chat_id, action, "failed", task=task, update_id=update_id, details=str(exc)
            )
            text, markup = self._task_detail(task_id)
            text = f"❌ {html.escape(str(exc))}\n\n{text}"
        if self.client:
            await self.client.edit_message(chat_id, message_id, text, markup)

    async def _send(self, chat_id: int, text: str, markup: dict[str, object] | None = None) -> None:
        if self.client:
            chunks = self._chunk_message(text)
            for index, chunk in enumerate(chunks):
                await self.client.send_message(
                    chat_id, chunk, markup if index == len(chunks) - 1 else None
                )

    @staticmethod
    def _chunk_message(text: str, max_chunk_size: int = 4000) -> list[str]:
        if len(text) <= max_chunk_size:
            return [text] if text else [""]
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + max_chunk_size, len(text))
            if end < len(text):
                amp = text.rfind("&", start, end)
                if amp != -1 and text.find(";", amp, end) == -1:
                    # Never let an entity boundary leave the cursor in place.
                    # A malicious/remote response may start with an entity
                    # longer than one Telegram chunk.
                    end = amp if amp > start else min(start + max_chunk_size, len(text))
                else:
                    nl = text.rfind("\n", start + max_chunk_size - 500, end)
                    if nl != -1:
                        end = nl + 1
            chunks.append(text[start:end])
            start = end
        return chunks or [""]

    def _welcome(self, user_id: int, chat_id: int) -> str:
        if self.management.is_bound(user_id, chat_id):
            role = self.management.binding_role(user_id, chat_id) or "anonymous"
            available = {
                item["command"]
                for item in self.command_configs()
                if BotManagementService._menu_visible(item) and role in item.get("allowedRoles", [])
            }
            actions = []
            if "tasks" in available:
                actions.append("发送 /tasks 查看任务")
            if "status" in available:
                actions.append("发送 /status 查看系统状态")
            if "checkin" in available:
                actions.append("发送 /checkin 领取每日积分")
            suffix = "\n\n" + "，".join(actions) + "。" if actions else ""
            role_label = "管理员" if self.management.is_admin(user_id, chat_id) else "普通用户"
            return f"👋 你已绑定{role_label}身份。{suffix}"
        return "👋 欢迎使用 tg-bot 管理 Bot。\n\n请使用后台生成的绑定码发送：\n<code>/bind ABCD-EFGH-IJKL</code>"

    def _help(self, user_id: int | None = None, chat_id: int | None = None) -> str:
        lines = ["<b>tg-bot 管理 Bot</b>"]
        role = (
            self.management.binding_role(user_id, chat_id) or "anonymous"
            if user_id is not None and chat_id is not None
            else None
        )
        for item in self.command_configs():
            if BotManagementService._menu_visible(item) and (
                role is None or role in item.get("allowedRoles", [])
            ):
                lines.append(f"/{item['command']} {html.escape(item['description'])}")
        return "\n\n".join(lines)

    def _system_status(self) -> str:
        accounts = self.database.list_accounts()
        since = utc_now() - timedelta(hours=24)
        stats = self.database.dashboard_stats(since)
        scheduler = self.checkin.scheduler.running
        lines = [
            "<b>系统状态</b>",
            f"服务：{'正常' if scheduler else '异常'}",
            "数据库：正常",
            f"调度器：{'运行中' if scheduler else '已停止'}",
            f"Telegram 账号：{sum(account.is_active for account in accounts)}/{len(accounts)} 活跃",
            f"任务：{stats['tasks_enabled']}/{stats['tasks_total']} 启用，{len(self.checkin.running)} 运行中",
            f"近 24 小时：成功 {stats['runs_success']}，失败 {stats['runs_failed']}，取消 {stats['runs_canceled']}",
            f"管理 Bot：{'运行中' if self.status.running else '未运行'}",
        ]
        if self.status.last_error:
            lines.append(f"Bot 最近错误：{html.escape(self.status.last_error)}")
        return "\n".join(lines)

    @staticmethod
    def _ids(user: dict[str, object], chat: dict[str, object]) -> tuple[int | None, int | None]:
        user_id = user.get("id")
        chat_id = chat.get("id")
        return (
            user_id if isinstance(user_id, int) else None,
            chat_id if isinstance(chat_id, int) else None,
        )

    @staticmethod
    def _page(value: str) -> int:
        try:
            return max(1, int(value))
        except ValueError:
            return 1

    @staticmethod
    def _format_time(value: datetime | None, timezone_name: str) -> str:
        return format_local_time(value, timezone_name, seconds=False)
