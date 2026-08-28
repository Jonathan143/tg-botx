from __future__ import annotations

from typing import Any

import uvicorn
from typer.testing import CliRunner

from tg_botx.interfaces import cli


class FakeSettings:
    api_host = "127.0.0.1"
    api_port = 8765

    def require_admin_config(self) -> None:
        pass


def test_serve_reload_uses_importable_app_factory(monkeypatch) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    result = CliRunner().invoke(cli.app, ["serve", "--reload"])

    assert result.exit_code == 0
    assert len(calls) == 1
    app, options = calls[0]
    assert app == cli.SERVER_APP_FACTORY
    assert options["factory"] is True
    assert options["reload"] is True
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 8765
    assert options["access_log"] is False
    assert options["timeout_graceful_shutdown"] == 5


def test_serve_disables_reload_by_default(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(uvicorn, "run", lambda _app, **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(cli.app, ["serve"])

    assert result.exit_code == 0
    assert calls[0]["reload"] is False
