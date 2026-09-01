"""Global safety guards for the test suite."""

import pytest


@pytest.fixture(autouse=True)
def disable_real_telegram_integrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit live Telegram credentials from the repository .env file."""
    monkeypatch.setenv("TG_BOT_NOTIFICATION_BOT_TOKEN", "")
    monkeypatch.setenv("TG_BOT_ADMIN_CHAT_IDS", "")
    monkeypatch.setenv("TG_BOT_ADMIN_BOT_TOKEN", "")
    monkeypatch.setenv("TG_BOT_BOT_ENABLED", "false")
    monkeypatch.setenv("TG_BOT_SERVICE_LIFECYCLE_NOTIFICATIONS_ENABLED", "false")
