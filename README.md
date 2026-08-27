# tg-bot

基于 Telegram 用户账号的签到调度服务，支持手机号/验证码、二维码登录，直接指令签到，以及等待消息后点击按钮的链式签到。

## 快速开始

```bash
cp .env.example .env
# 填入 https://my.telegram.org 获取的 api_id/api_hash，并配置后台密钥与 Origin
# TG_BOT_ADMIN_KEY 建议使用 `openssl rand -base64 48` 生成
python -m venv .venv
source .venv/bin/activate
pip install -e .

tg-bot login --method qr
tg-bot task create --config examples/direct.yaml
tg-bot serve
```

`serve` 会在同一 asyncio 生命周期中启动 APScheduler、Telethon 客户端池和 FastAPI
后台 API。未配置 `TG_BOT_ADMIN_KEY`/`TG_BOT_ADMIN_ORIGIN` 或密钥少于 32 个 UTF-8 字节时，
管理 API 会拒绝启动。

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

## 后台管理 API

后台假定始终由 HTTPS 反向代理提供同源 `/api`：

```text
TG_BOT_ADMIN_KEY=<openssl rand -base64 48 的输出>
TG_BOT_ADMIN_ORIGIN=https://admin.example.com
TG_BOT_ADMIN_SESSION_DAYS=30
TG_BOT_API_HOST=0.0.0.0
TG_BOT_API_PORT=8000
TG_BOT_TRANSPORT_KEY_ROTATION_HOURS=24
# 仅当前置反向代理可信时显式填写 CIDR
TG_BOT_TRUSTED_PROXIES=
```

`GET /api/auth/key` 返回内存 RSA 公钥和一次性 nonce。前端将所有敏感值编码为 UTF-8 JSON
`{value:string,nonce:string,timestamp:string}`，再用 RSA-OAEP/SHA-256 加密。登录成功后使用
`HttpOnly+Secure+SameSite=Strict` Cookie 和内存 CSRF Token。除公钥获取和验证外，
OpenAPI 与全部管理路由均需认证。
`POST /api/settings/transport-key/rotate` 可立即轮换传输密钥，旧私钥仅保留 5 分钟宽限期。

任务详情支持 `GET /api/tasks/:id/events` SSE 流。该接口复用管理后台的登录 Cookie，
不要求 CSRF Token；连接建立后立即发送一次当前任务快照，随后在任务配置、调度或运行状态
变化时发送 `task.updated` 事件。每条事件的 `data` 都是与 `GET /api/tasks/:id`
一致的完整 Task JSON，并带有进程内单调递增的 `id`。服务每 15 秒发送一次
`: keepalive` 注释，响应包含 `X-Accel-Buffering: no`，反向代理也应关闭该路由的响应缓冲。
客户端断开时服务会释放对应任务的订阅。

Compose 配置只通过 `expose` 向同一 Compose 网络公布 8000 端口，没有宿主机
`ports` 映射。请将 Web 反向代理加入同一网络，由它将同源 `/api` 转发到
`http://tg-bot:8000`；不应直接将 Uvicorn 端口暴露到公网。

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
