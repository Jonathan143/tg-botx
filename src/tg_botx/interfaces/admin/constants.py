import re

SESSION_COOKIE = "tg_bot_admin_session"

CSRF_HEADER = "X-CSRF-Token"

ADMIN_BOT_WEBHOOK_PATH = "/api/telegram/admin-bot/webhook"

TASK_EVENT_KEEPALIVE_SECONDS = 15

LOG_EVENT_POLL_SECONDS = 1

_LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T][^ ]+)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>\S+)\s*(?P<message>.*)$"
)
