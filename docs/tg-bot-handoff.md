# tg-bot 交接文档

## 接手目标

继续完善并验证当前 Telegram 用户账号签到 MVP。产品设计已经通过多轮 grilling 确认，下一步重点是安装依赖后做真实运行链路检查、修复发现的问题，并根据需要继续实现功能。

## 当前状态

- 工作区：`/Users/Kang/Documents/ChatGPT/tg-bot`
- 仓库原本只有 `.git`，目前已搭建完整 Python MVP 骨架。
- 没有执行 Git commit；不要自行 commit。
- 已执行：`python3 -m compileall -q src`、`rtk git diff --check`，均通过。
- 未执行测试用例和 eslint，符合仓库要求。
- 当前环境没有安装项目依赖；`PYTHONPATH=src python3 -c 'import tg_bot.cli'` 因缺少 `typer` 失败。尚未安装依赖，也没有连接 Telegram 做真实验证。

## 设计依据

完整设计共识已经在本次对话中确认；代码、示例配置和运行方式分别见：

- `/Users/Kang/Documents/ChatGPT/tg-bot/README.md`
- `/Users/Kang/Documents/ChatGPT/tg-bot/pyproject.toml`
- `/Users/Kang/Documents/ChatGPT/tg-bot/examples/direct.yaml`
- `/Users/Kang/Documents/ChatGPT/tg-bot/examples/chain.yaml`

核心约定：Python 3.12+、Telethon 用户账号 MTProto、APScheduler、SQLAlchemy + SQLite/PostgreSQL、Typer、Pydantic、Docker；支持手机号/验证码和二维码登录；任务支持直接指令、消息等待、按钮点击链；固定每日时间或指定时区内随机到秒；失败重试、执行历史、管理员 Telegram 失败通知；同账号同聊天串行执行。

## 代码入口

- 配置：`/Users/Kang/Documents/ChatGPT/tg-bot/src/tg_bot/config.py`
- 数据模型：`/Users/Kang/Documents/ChatGPT/tg-bot/src/tg_bot/db.py`
- YAML 校验：`/Users/Kang/Documents/ChatGPT/tg-bot/src/tg_bot/schemas.py`
- 登录：`/Users/Kang/Documents/ChatGPT/tg-bot/src/tg_bot/auth.py`
- 消息/按钮执行器：`/Users/Kang/Documents/ChatGPT/tg-bot/src/tg_bot/executor.py`
- 匹配：`/Users/Kang/Documents/ChatGPT/tg-bot/src/tg_bot/matching.py`
- 时间计算：`/Users/Kang/Documents/ChatGPT/tg-bot/src/tg_bot/schedule.py`
- 调度、重试、通知、取消：`/Users/Kang/Documents/ChatGPT/tg-bot/src/tg_bot/runtime.py`
- CLI：`/Users/Kang/Documents/ChatGPT/tg-bot/src/tg_bot/cli.py`

## 建议下一步

1. 阅读上述入口文件，检查依赖 API 兼容性，尤其是 Telethon QR 登录、消息编辑事件和按钮点击 API。
2. 在用户允许并提供凭证后，执行 `cp .env.example .env`、填写 Telegram API 凭证，再创建虚拟环境并运行 `pip install -e .`。
3. 运行 `tg-bot login --method qr`，验证 session 持久化和账号恢复。
4. 使用示例 YAML 创建、启用并手动运行任务；真实验证直接指令、链式按钮、超时、重试和随机到秒调度。
5. 修复真实运行中发现的问题；不要写测试用例、不要执行 eslint、不要本地做视觉/交互验收，除非用户明确要求。

## 注意事项

- 不要在交接文档、日志、回复或提交中暴露 API key、API hash、手机号、验证码、二次验证密码或 Telegram session 内容。
- `.env`、`data/` 和 session 文件已加入 `.gitignore`，不要提交。
- CLI 依赖账号先登录；任务创建后默认停用，需要显式 `tg-bot task enable <task-id>`。
- 用户尚未要求提交 Git；如未来需要 commit，必须使用简体中文 Conventional Commits 标题，且先得到用户明确指令。

## Suggested skills

- 当前不需要额外技能；产品设计已经确认。
- 如果需要重新审查或扩展产品决策，调用 `grilling` skill。
- 如果用户要求本地页面视觉或交互验收，才调用相应的产品设计/浏览器技能；当前不要主动验收 UI。
