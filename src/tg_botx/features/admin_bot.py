"""旧管理机器人导入路径的兼容导出。"""

from tg_botx.features.bot.management import BotManagementService as BotManagementService
from tg_botx.features.bot.models import DEFAULT_BOT_COMMANDS as DEFAULT_BOT_COMMANDS
from tg_botx.features.bot.models import BindingCodeView as BindingCodeView
from tg_botx.features.bot.models import BotBindingError as BotBindingError
from tg_botx.features.bot.models import BotCommandConflictError as BotCommandConflictError
from tg_botx.features.bot.models import BotCommandForbiddenError as BotCommandForbiddenError
from tg_botx.features.bot.models import BotCommandValidationError as BotCommandValidationError
from tg_botx.features.bot.models import BotRuntimeStatus as BotRuntimeStatus
from tg_botx.features.bot.models import hash_binding_code as hash_binding_code
from tg_botx.features.bot.models import normalize_binding_code as normalize_binding_code
from tg_botx.interfaces.telegram.runtime import TelegramManagementBot as TelegramManagementBot
