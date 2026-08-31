# tg-bot

基于 Telegram 用户账号的签到调度服务，支持手机号/验证码、二维码登录，直接指令签到，以及等待消息后点击按钮的链式签到。

项目使用 Python 3.12+、`src/` 布局和显式分层设计。当前默认启用签到调度；机器人管理、频道通知和群监控以独立 feature/adapter 形式预留，便于逐步上线而不影响现有任务。

架构说明见 [`docs/architecture.md`](docs/architecture.md)。
业务代码统一从 `tg_botx.infrastructure`、`tg_botx.features` 和 `tg_botx.interfaces` 子包导入。

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

开发时可使用 `tg-bot serve --reload` 监控 Python 文件变化并自动重启服务。该参数仅适合
本地开发，不应在生产环境中启用。

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

日志默认同时输出到终端和 `data/logs/tg-bot.log`，并按 10 MB 自动轮转，保留 5 个历史文件；终端和文件日志会按级别加图标，便于快速扫读。可通过以下环境变量调整：

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
`HttpOnly+Secure+SameSite=Strict` Cookie；会话摘要持久化在数据库中，因此服务重启后浏览器无需
重新输入管理密钥（直到会话过期或主动退出）。CSRF Token 从会话 Cookie 派生且不会明文落库。
除公钥获取和验证外，OpenAPI 与全部管理路由均需认证。
`POST /api/settings/transport-key/rotate` 可立即轮换传输密钥，旧私钥仅保留 5 分钟宽限期。

`POST /api/tasks/:id/run` 成功时返回更新后的完整 Task JSON（HTTP 202），其中
`running=true`，前端可据此立即连接 `GET /api/tasks/:id/events` SSE 流。SSE 接口复用
管理后台的登录 Cookie，不要求 CSRF Token；连接建立后立即发送一次当前任务快照，随后在
任务配置、调度、运行或步骤状态变化时发送 `task.updated` 事件。每条事件的 `data` 都与
`GET /api/tasks/:id` 的完整 Task JSON 一致，并带有进程内单调递增的 `id`。

Task JSON 的 `run` 为当前或本进程内最近一次运行进度；从未运行时为 `null`：

```json
{
  "running": true,
  "run": {
    "id": "run-uuid",
    "status": "running",
    "attempt": 1,
    "stepStatuses": [
      {"index": 0, "status": "success"},
      {"index": 1, "status": "running"},
      {"index": 2, "status": "pending"}
    ]
  }
}
```

步骤 `status` 取值为 `pending`、`running`、`success`、`failed` 或 `skipped`，失败步骤
可带 `error` 和节点耗时 `durationMs`；`wait_message` 步骤收到消息后会在步骤状态中带上脱敏后的 `botResponse`，以及按 Telegram 原始行排列的
`botButtons`（仅包含按钮显示文字）；运行级 `run.status` 取值为 `running`、`success`、`failed`、`canceled`
或 `skipped`，失败或取消时可带 `error`。`attempt=0` 表示运行已预留、执行器尚未开始；
重试开始时 `attempt` 增加，步骤状态按新 attempt 重置。运行结束的最后一次事件保留最终
步骤状态。服务每 15 秒发送一次 `: keepalive`
注释，响应包含 `X-Accel-Buffering: no`，反向代理也应关闭该路由的响应缓冲。客户端断开
时服务会释放对应任务的订阅。

任务工作流分为可编辑的 `main` 草稿和手动发布的不可变版本（`v1`、`v2`……）。只有发布后
才能启用任务并进入正式调度；正式运行记录只保存发布版本号，详情通过版本表读取固定定义。
编辑页的“测试工作流”可直接执行当前未保存内容，测试记录保存执行瞬间的工作流快照，且不改变
任务的正式运行状态。

Compose 配置只通过 `expose` 向同一 Compose 网络公布 8000 端口，没有宿主机
`ports` 映射。请将 Web 反向代理加入同一网络，由它将同源 `/api` 转发到
`http://tg-bot:8000`；不应直接将 Uvicorn 端口暴露到公网。

## GitHub Actions 发布 Docker 镜像

`.github/workflows/docker-publish.yml` 会使用 GitHub Actions 构建并发布多架构镜像到
GitHub Container Registry（GHCR），镜像地址为
`ghcr.io/<github 用户名>/<仓库名>`，支持 `linux/amd64` 和 `linux/arm64`。

- 只有推送 Git tag 时才会运行工作流；普通分支 push、Pull Request 和手动运行都不会构建。
- 每个 tag 都会发布同名镜像标签、`latest` 和提交 SHA 标签；符合 SemVer 的 `v*` 标签
  （例如 `v1.2.3`）还会发布 `1.2.3` 和 `1.2` 标签。

工作流使用自动生成的 `GITHUB_TOKEN`，仓库无需额外配置密码；请确保仓库的 Actions 已启用，
并允许工作流写入 Packages（工作流已声明 `packages: write` 权限）。发布后可按需设置 GHCR
包的可见性，然后使用对应标签拉取：

```bash
docker pull ghcr.io/<github 用户名>/<仓库名>:latest
```

## Release 发布脚本

使用 [`scripts/release.sh`](scripts/release.sh) 可自动递增项目版本、提交版本变更、推送当前分支，
再创建并推送对应的 `v<版本>` tag，从而触发上面的镜像发布工作流。默认递增 patch 版本：

```bash
./scripts/release.sh              # 例如 0.0.1 -> 0.0.2
./scripts/release.sh major        # 例如 0.0.1 -> 1.0.0
./scripts/release.sh minor        # 例如 0.0.1 -> 0.1.0
./scripts/release.sh patch        # 例如 0.0.1 -> 0.0.2
```

脚本默认推送到 `origin`，也可通过 `RELEASE_REMOTE=<远程名>` 指定其他远程。为避免误提交，
执行前需要保持工作区干净，并确保当前分支不是 detached HEAD。

## Telegram 机器人通知

通知通过 BotFather 创建的独立机器人和 Telegram Bot API 发送，不复用签到用户账号的 session：

```text
TG_BOT_NOTIFICATION_BOT_TOKEN=replace-with-bot-token
TG_BOT_ADMIN_CHAT_IDS=123456789
# 服务启动/停止等通知的默认时间展示时区；任务结果通知优先使用任务 schedule.timezone
TG_BOT_NOTIFICATION_TIMEZONE=Asia/Shanghai
# 开发环境可关闭服务启动/停止通知（不影响任务结果和致命异常通知）
TG_BOT_SERVICE_LIFECYCLE_NOTIFICATIONS_ENABLED=false
```

### Telegram 管理 Bot

交互式任务管理 Bot 使用独立的 BotFather Token，不要将它与后台 API 密钥
`TG_BOT_ADMIN_KEY` 混用：

```text
TG_BOT_ADMIN_BOT_TOKEN=replace-with-management-bot-token
TG_BOT_BOT_ENABLED=true
```

启用后 Bot 通过长轮询运行在 `serve` 进程中，仅接受私聊。管理员在 Web 后台“设置”页生成
一次性绑定码，也可以使用 `tg-bot bot binding create`，然后在 Bot 中发送
`/bind ABCD-EFGH-IJKL`。绑定成功后可使用 `/tasks` 分页查看、启用、停用和手动执行任务，
使用 `/status` 查看脱敏系统状态。绑定码默认 10 分钟有效且只能使用一次；后台可撤销绑定。

管理员必须先在 Telegram 中打开该机器人并发送 `/start`，否则机器人不能主动发起私聊。通知只发送到
`TG_BOT_ADMIN_CHAT_IDS` 中的第一个 chat ID；未配置 Token 或管理员 chat ID 时，通知功能保持禁用并记录警告，
不会影响签到任务执行。

机器人通知包括任务最终成功或失败、取消请求与实际取消、忙碌跳过，以及 `serve` 常驻服务的启动、
SIGINT/SIGTERM 优雅停止和可捕获的致命异常。通过 `TG_BOT_SERVICE_LIFECYCLE_NOTIFICATIONS_ENABLED=false`
可关闭服务启动/停止通知（适合开发环境），不影响任务和致命异常通知。投递发生网络错误、限流或服务端错误时最多重试 3 次；
最终投递失败只写日志，不改变签到任务结果。通知使用图标区分状态，任务事件按任务时区显示，服务事件使用
`TG_BOT_NOTIFICATION_TIMEZONE`（默认 `Asia/Shanghai`）。开启 `notify_bot_response` 后，通知会附带最后一次机器人回复；
日志对应使用 `log_bot_response`，两者默认关闭并继续执行敏感信息脱敏。

## CLI

```text
tg-bot login [--method phone|qr]
tg-bot logout
tg-bot task create --config <yaml>
tg-bot task list
tg-bot task validate <task-id>
tg-bot task publish <task-id> [--note <发布说明>]
tg-bot task enable <task-id>
tg-bot task disable <task-id>
tg-bot task run <task-id>
tg-bot task cancel <task-id>
tg-bot task history <task-id>
tg-bot task export <task-id> --output task.yaml
tg-bot task import --config task.yaml
tg-bot serve [--reload]
```

## 开发与代码规范

安装开发依赖并执行完整检查：

```bash
make dev
make check
```

项目统一使用 Ruff 格式化与静态检查、mypy 类型检查和 pytest 测试。业务代码放在 `src/tg_botx`，测试放在 `tests`；新增环境变量时同步更新 `Settings`、`.env.example` 和文档。

### 后续能力开关

以下开关默认关闭，开启前请先完成对应 Telegram 账号、权限和限流策略配置：

```text
TG_BOT_BOT_ENABLED=false
TG_BOT_CHANNEL_NOTIFICATIONS_ENABLED=false
TG_BOT_GROUP_MONITOR_ENABLED=false
TG_BOT_GROUP_MONITOR_HISTORY_LIMIT=500
```

`tg_botx.features` 提供可复用的 `CommandRegistry`、`ChannelNotifier` 和 `GroupMonitor`；它们只依赖抽象 transport/handler，可在管理 API、常驻服务或独立 worker 中组装。

任务配置采用 YAML，步骤仅允许内置类型，不执行任意 Python 或 Shell。
YAML 与管理 API 中的任务定义统一使用 `snake_case` 字段名，例如
`max_attempts`、`timeout_seconds`、`text_contains` 和 `callback_data`，不接受 camelCase 别名。

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

回复超过单条 Telegram 消息长度时，通知会自动分段发送。
