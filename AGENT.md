# AGENT.md

## 项目概览

`tg-bot` 是一个基于 Telegram 用户账号的签到调度服务。项目使用 Python 3.12+，核心依赖为 Telethon、APScheduler、SQLAlchemy、Pydantic、Typer 和 PyYAML，采用 `src/` 布局。

主要能力包括：

- 手机号/验证码或二维码登录 Telegram 用户账号，并持久化 session。
- 通过 YAML 定义直接发送消息、等待回复和点击按钮的签到步骤。
- 按固定时间或指定时间窗口内的随机时间执行任务。
- 支持失败重试、执行历史、任务取消以及 Telegram 管理员通知。
- 同一账号、同一目标聊天中的任务串行执行。

## 常用命令

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

tg-bot login --method qr
tg-bot task create --config examples/direct.yaml
tg-bot task validate <task-id>
tg-bot task enable <task-id>
tg-bot task run <task-id>
tg-bot serve
```

## 目录与模块职责

- `src/tg_bot/config.py`：环境变量、数据目录及日志配置。
- `src/tg_bot/schemas.py`：任务 YAML 的 Pydantic 模型和输入校验。
- `src/tg_bot/db.py`：SQLAlchemy 模型、SQLite/PostgreSQL 初始化与数据访问。
- `src/tg_bot/auth.py`：手机号和二维码登录、退出登录及 session 管理。
- `src/tg_bot/executor.py`：消息发送、回复等待、按钮点击和失败重试。
- `src/tg_bot/matching.py`：消息与按钮匹配规则。
- `src/tg_bot/schedule.py`：固定/随机调度时间计算和时区转换。
- `src/tg_bot/runtime.py`：客户端池、任务调度、并发锁、通知和取消。
- `src/tg_bot/cli.py`：Typer CLI 入口和日志初始化。
- `examples/`：可直接参考的任务 YAML。
- `data/`：SQLite 数据库（使用 PostgreSQL 时仅保存 Telegram session 和运行日志）；属于本地持久化数据，不得提交。

## 实现约定

- 保持 Python 3.12+ 兼容，优先使用现有类型标注和标准库能力。
- 延续当前模块边界；CLI 只负责编排和用户输出，业务逻辑放入对应服务模块。
- 所有持久化时间统一按 UTC 处理，并保持 timezone-aware；展示或计算计划时间时再使用任务配置的时区。
- 新增或修改 YAML 字段时，同步更新 `TaskDefinition`/相关 Pydantic 模型、序列化逻辑、示例和 README。输入模型继续使用 `extra="forbid"`，避免静默接受拼写错误。
- 任务步骤只能使用显式允许的内置类型。不得引入执行任意 Python、Shell 或不受控表达式的能力。
- Telethon 调用保持异步；客户端应通过现有 `ClientPool` 复用，并在退出时可靠断开。
- 保持同一账号与目标聊天的串行语义，修改调度或执行流程时不得绕过 `(account.id, target)` 锁。
- 数据库写入通过 `Database` 封装完成；SQLite 仍需保留 `check_same_thread=False` 和现有 UTC 转换行为，PostgreSQL 使用 psycopg 驱动并保持 UTC 时区语义。
- 数据库由 `TG_BOT_DATABASE` 选择（`sqlite` 或 `postgresql`）；选择 PostgreSQL 时必须配置 `TG_BOT_DATABASE_URL`，SQLite 默认使用 `TG_BOT_DATA_DIR/database.sqlite3`。
- 面向 CLI 用户的提示、异常信息和项目文档使用简体中文；日志应包含必要的任务标识，但不得包含敏感信息。
- 保持改动聚焦，不顺带重构无关代码，也不要覆盖用户已有的工作区改动。

## 安全与数据约束

- 不得读取、打印、记录或提交 `.env` 中的真实凭证。
- 不得暴露 Telegram API ID/API hash、手机号、验证码、二次验证密码、管理员 chat ID 或 session 内容。
- 不得提交 `.env`、`data/`、`.venv/`、`*.session`、数据库文件、日志或缓存产物。
- 示例和文档只能使用明显的占位值。
- 涉及真实 Telegram 登录、发送消息或签到时，先确认用户明确要求，并说明这会产生外部副作用。

## 修改后的检查

除非用户明确要求，不新增测试用例、不执行测试套件、不执行 ESLint，也不进行本地页面视觉或交互验收。

可按改动范围执行轻量检查：

```bash
python3 -m compileall -q src
rtk git diff --check
```

若依赖尚未安装，不要擅自安装或填写 Telegram 凭证；说明未能执行依赖相关验证即可。真实 Telegram 链路只能在用户授权并提供可用配置后验证。

## Git 约束

- 未经用户明确命令，不得执行 `git commit`、推送、变基或其他会改写历史的操作。
- 用户要求生成 commit message 时，必须遵循 Conventional Commits：`type` 和可选 `scope` 使用英文，标题与正文使用简体中文，仅不可翻译的技术术语保留英文。
- 示例：`fix(runtime): 修复失败任务未重新安排的问题`。
