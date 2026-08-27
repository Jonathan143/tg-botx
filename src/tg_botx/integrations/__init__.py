"""External-service adapters.

Adapters live behind feature protocols so core services can be tested without
network access or a real Telegram account.
"""

from tg_botx.integrations.telegram import (
    TelegramAccountConfig,
    TelegramClientProvider,
    create_telethon_client,
)

__all__ = ["TelegramAccountConfig", "TelegramClientProvider", "create_telethon_client"]
