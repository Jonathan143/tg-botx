from __future__ import annotations

from typing import Any

from tg_botx.features.bot.executors.base import CommandContext, ExecutionResult
from tg_botx.features.bot.executors.schemas import HttpConfig
from tg_botx.features.bot.executors.templates import render
from tg_botx.integrations.safe_http import SafeHttpClient, extract_text


class HttpExecutor:
    def __init__(self, client: SafeHttpClient):
        self.client = client

    async def execute(self, config: dict[str, Any], context: CommandContext) -> ExecutionResult:
        parsed = HttpConfig.model_validate(config)
        payload = await self.client.request(
            parsed,
            query=render(parsed.query, context),
            headers=render(parsed.headers, context),
            json_body=render(parsed.json_body, context),
            text_body=render(parsed.text_body, context),
        )
        return ExecutionResult(extract_text(payload, parsed)).validated()

    async def close(self) -> None:
        await self.client.close()
