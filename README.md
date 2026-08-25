# tg-bot

基于 Telegram 用户账号的签到调度服务，支持手机号/验证码、二维码登录，直接指令签到，以及等待消息后点击按钮的链式签到。

## 快速开始

```bash
cp .env.example .env
# 填入 https://my.telegram.org 获取的 api_id 和 api_hash
python -m venv .venv
source .venv/bin/activate
pip install -e .

tg-bot login --method qr
tg-bot task create --config examples/direct.yaml
tg-bot serve
```

`data/` 中包含 SQLite 数据库（以及 Telegram session 和日志），必须持久化并避免提交到 Git。

数据库默认使用 SQLite。可通过环境变量切换到 PostgreSQL：

```text
TG_BOT_DATABASE=sqlite
# TG_BOT_DATABASE=postgresql
# TG_BOT_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/tg_bot
```

选择 PostgreSQL 时必须配置 `TG_BOT_DATABASE_URL`。连接串也支持常见的
`postgresql://` 或 `postgres://` 写法，程序会自动使用 psycopg 驱动；SQLite
仍使用 `TG_BOT_DATA_DIR/database.sqlite3`。

日志默认同时输出到终端和 `data/logs/tg-bot.log`，并按 10 MB 自动轮转，保留 5 个历史文件。可通过以下环境变量调整：

```text
TG_BOT_LOG_LEVEL=INFO
TG_BOT_LOG_FILE=tg-bot.log
TG_BOT_LOG_MAX_BYTES=10485760
TG_BOT_LOG_BACKUP_COUNT=5
```

## Telegram 机器人通知

通知通过 BotFather 创建的独立机器人和 Telegram Bot API 发送，不复用签到用户账号的 session：

```text
TG_BOT_NOTIFICATION_BOT_TOKEN=replace-with-bot-token
TG_BOT_ADMIN_CHAT_IDS=123456789
```

管理员必须先在 Telegram 中打开该机器人并发送 `/start`，否则机器人不能主动发起私聊。通知只发送到
`TG_BOT_ADMIN_CHAT_IDS` 中的第一个 chat ID；未配置 Token 或管理员 chat ID 时，通知功能保持禁用并记录警告，
不会影响签到任务执行。

机器人通知包括任务最终成功或失败、取消请求与实际取消、忙碌跳过，以及 `serve` 常驻服务的启动、
SIGINT/SIGTERM 优雅停止和可捕获的致命异常。投递发生网络错误、限流或服务端错误时最多重试 3 次；
最终投递失败只写日志，不改变签到任务结果。任务事件按任务时区显示，服务事件使用 UTC。

## CLI

```text
tg-bot login [--method phone|qr]
tg-bot logout
tg-bot task create --config <yaml>
tg-bot task list
tg-bot task validate <task-id>
tg-bot task enable <task-id>
tg-bot task disable <task-id>
tg-bot task run <task-id>
tg-bot task cancel <task-id>
tg-bot task history <task-id>
tg-bot task export <task-id> --output task.yaml
tg-bot task import --config task.yaml
tg-bot serve
```

任务配置采用 YAML，步骤仅允许内置类型，不执行任意 Python 或 Shell。

可在任务中分别控制失败和成功通知。未配置 `notifications` 时默认仅发送失败通知：

```yaml
notifications:
  failure: true
  success: false
```

将某个维度设为 `false` 可关闭对应的最终结果通知。取消、忙碌跳过和服务生命周期通知由全局规则发送。

机器人最后一次回复可能包含账号、余额、积分或兑换码等敏感信息，因此日志和通知分别使用独立开关，
并且默认都关闭：

```yaml
log_bot_response: false
notify_bot_response: false
```

旧配置 `output_bot_response` 仍兼容：未配置对应新开关时，它会同时控制日志和通知输出。新开关优先于旧配置。
回复超过单条 Telegram 消息长度时，通知会自动分段发送。
