"""Small asynchronous Telegram Bot API client for interactive integrations."""

from __future__ import annotations

import httpx


class TelegramBotApiError(RuntimeError):
    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TelegramBotApiClient:
    """Bot API transport with no dependency on the management feature."""

    def __init__(self, token: str):
        self._token = token.strip()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(35.0, connect=5.0),
            headers={"User-Agent": "tg-checkin-bot/0.1"},
        )
        # The token is embedded in the request path; never let httpx log it.
        import logging

        logging.getLogger("httpx").setLevel(logging.WARNING)

    async def call(self, method: str, payload: dict[str, object] | None = None) -> object:
        response = await self._client.post(
            f"https://api.telegram.org/bot{self._token}/{method}",
            json=payload or {},
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict) or response.status_code != 200 or body.get("ok") is not True:
            parameters = body.get("parameters", {}) if isinstance(body, dict) else {}
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            raise TelegramBotApiError(
                f"Telegram Bot API 调用失败：{method} ({response.status_code})",
                retry_after=int(retry_after) if isinstance(retry_after, (int, float)) else None,
            )
        return body.get("result")

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, object]]:
        payload: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self.call(
            "getUpdates",
            payload,
        )
        return (
            [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []
        )

    async def set_webhook(self, url: str, secret_token: str) -> object:
        return await self.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": True,
            },
        )

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> object:
        return await self.call(
            "deleteWebhook",
            {"drop_pending_updates": drop_pending_updates},
        )

    async def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, object] | None = None
    ) -> object:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", payload)

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> object:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.call("editMessageText", payload)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> object:
        payload: dict[str, object] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        return await self.call("answerCallbackQuery", payload)

    async def set_commands(
        self,
        commands: list[dict[str, str]],
        *,
        scope: dict[str, object] | None = None,
        language_code: str | None = None,
    ) -> object:
        payload: dict[str, object] = {"commands": commands}
        if scope is not None:
            payload["scope"] = scope
        if language_code is not None:
            payload["language_code"] = language_code
        return await self.call("setMyCommands", payload)

    async def delete_commands(
        self,
        *,
        scope: dict[str, object] | None = None,
        language_code: str | None = None,
    ) -> object:
        payload: dict[str, object] = {}
        if scope is not None:
            payload["scope"] = scope
        if language_code is not None:
            payload["language_code"] = language_code
        return await self.call("deleteMyCommands", payload)

    async def get_commands(
        self,
        *,
        scope: dict[str, object] | None = None,
        language_code: str | None = None,
    ) -> list[dict[str, str]]:
        payload: dict[str, object] = {}
        if scope is not None:
            payload["scope"] = scope
        if language_code is not None:
            payload["language_code"] = language_code
        result = await self.call("getMyCommands", payload)
        return (
            [
                item
                for item in result
                if isinstance(item, dict)
                and isinstance(item.get("command"), str)
                and isinstance(item.get("description"), str)
            ]
            if isinstance(result, list)
            else []
        )

    async def close(self) -> None:
        await self._client.aclose()
