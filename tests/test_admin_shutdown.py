from __future__ import annotations

import asyncio
import signal
from typing import Any

from tg_botx.interfaces.admin import admin_api


def test_shutdown_signal_notifies_streams_and_delegates(monkeypatch) -> None:
    main_thread = object()
    delegated: list[tuple[int, Any]] = []
    installed: dict[signal.Signals, Any] = {}

    def previous(signum: int, frame: Any) -> None:
        delegated.append((signum, frame))

    monkeypatch.setattr(admin_api.threading, "main_thread", lambda: main_thread)
    monkeypatch.setattr(admin_api.threading, "current_thread", lambda: main_thread)
    monkeypatch.setattr(admin_api.signal, "getsignal", lambda _received: previous)
    monkeypatch.setattr(
        admin_api.signal,
        "signal",
        lambda received, handler: installed.__setitem__(received, handler),
    )
    shutdown_event = asyncio.Event()

    previous_handlers = admin_api._install_shutdown_signal_handlers(shutdown_event)
    frame = object()
    installed[signal.SIGTERM](signal.SIGTERM, frame)

    assert shutdown_event.is_set()
    assert delegated == [(signal.SIGTERM, frame)]
    assert previous_handlers == {signal.SIGINT: previous, signal.SIGTERM: previous}

    admin_api._restore_signal_handlers(previous_handlers)
    assert installed == {signal.SIGINT: previous, signal.SIGTERM: previous}
