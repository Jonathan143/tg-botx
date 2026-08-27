"""Extensible command dispatching for a future Telegram bot account.

Handlers are plain async callables, which keeps this module independent from
Telethon.  A Telethon adapter only needs to turn an incoming event into a
``CommandContext`` and pass it to :meth:`CommandRegistry.dispatch`.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Normalized incoming command data."""

    command: str
    args: tuple[str, ...] = ()
    text: str = ""
    chat_id: int | str | None = None
    sender_id: int | str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class CommandHandler(Protocol):
    def __call__(self, context: CommandContext) -> str | None | Awaitable[str | None]: ...


class CommandRegistry:
    """Register and dispatch explicit commands without dynamic code execution."""

    def __init__(self, prefix: str = "/") -> None:
        if not prefix or any(char.isspace() for char in prefix):
            raise ValueError("命令前缀不能为空且不能包含空白字符")
        self.prefix = prefix
        self._handlers: dict[str, CommandHandler] = {}
        self._aliases: dict[str, str] = {}

    @property
    def commands(self) -> tuple[str, ...]:
        """Return registered command names in deterministic order."""

        return tuple(sorted(self._handlers))

    def register(
        self, name: str, *, aliases: tuple[str, ...] = ()
    ) -> Callable[[CommandHandler], CommandHandler]:
        normalized = self._normalize_name(name)
        normalized_aliases = tuple(self._normalize_name(alias) for alias in aliases)
        if normalized in normalized_aliases or len(set(normalized_aliases)) != len(
            normalized_aliases
        ):
            raise ValueError("命令别名不能与命令名称或彼此重复")

        def decorator(handler: CommandHandler) -> CommandHandler:
            if normalized in self._handlers or normalized in self._aliases:
                raise ValueError(f"命令已注册：{normalized}")
            if any(alias in self._handlers or alias in self._aliases for alias in normalized_aliases):
                raise ValueError("命令别名已注册")
            self._handlers[normalized] = handler
            for alias in normalized_aliases:
                self._aliases[alias] = normalized
            return handler

        return decorator

    async def dispatch(self, text: str, **context: Any) -> str | None:
        """Dispatch one message; return ``None`` when it is not a command."""

        parsed = self.parse(text)
        if parsed is None:
            return None
        command, args = parsed
        canonical = self._aliases.get(command, command)
        handler = self._handlers.get(canonical)
        if handler is None:
            return None
        result = handler(CommandContext(command=canonical, args=args, text=text, **context))
        return await result if inspect.isawaitable(result) else result

    def parse(self, text: str) -> tuple[str, tuple[str, ...]] | None:
        stripped = text.strip()
        if not stripped.startswith(self.prefix):
            return None
        body = stripped[len(self.prefix) :].strip()
        if not body:
            return None
        parts = body.split()
        # Telegram appends ``@bot_username`` when a command targets a bot in
        # a group.  The registry intentionally treats that suffix as routing
        # metadata rather than part of the command name.
        command = parts[0].split("@", 1)[0]
        return self._normalize_name(command), tuple(parts[1:])

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip().lstrip("/").casefold()
        if not normalized or any(char.isspace() for char in normalized):
            raise ValueError("命令名称不能为空且不能包含空白字符")
        return normalized
