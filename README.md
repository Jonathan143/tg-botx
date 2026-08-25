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

`data/` 中包含 SQLite 数据库、Telegram session 和日志，必须持久化并避免提交到 Git。

日志默认同时输出到终端和 `data/logs/tg-bot.log`，并按 10 MB 自动轮转，保留 5 个历史文件。可通过以下环境变量调整：

```text
TG_BOT_LOG_LEVEL=INFO
TG_BOT_LOG_FILE=tg-bot.log
TG_BOT_LOG_MAX_BYTES=10485760
TG_BOT_LOG_BACKUP_COUNT=5
```

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

通知会发送到 `TG_BOT_ADMIN_CHAT_IDS` 配置的 Telegram chat ID；多个 ID 使用逗号分隔。将某个维度设为 `false` 可关闭对应通知。

如需同时在运行日志和通知中输出机器人最后一次回复的完整消息体，可开启：

```yaml
output_bot_response: true
```

该配置默认关闭。回复超过单条 Telegram 消息长度时，通知会自动分段发送。
