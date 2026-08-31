# 架构与扩展约定

项目采用 `src` 布局，并按职责划分为多个边界清晰的层次：

```text
src/tg_botx/
├── application/           # 进程级依赖组装
│   └── container.py       # CLI/API/worker 共用的对象图
├── interfaces/            # 用户可见入口
│   ├── cli.py             # Typer 命令行适配器
│   └── admin/             # FastAPI 管理 API、账号和安全
├── core/                  # 与 Telegram/数据库无关的稳定契约
│   └── events.py          # 进程内领域事件总线
├── features/              # 面向业务能力的可组合服务
│   ├── checkin/           # 签到执行、匹配、调度和运行时
│   ├── accounts/          # Telegram 账号认证
│   ├── commands.py        # 自定义命令注册与分发
│   ├── channel.py         # 频道通知契约
│   └── monitoring.py      # 群消息规则、历史窗口和总结接口
├── infrastructure/       # 外部系统和基础设施适配器
│   ├── persistence/       # SQLAlchemy 模型和数据库访问
│   └── observability/     # 日志与敏感数据脱敏
├── integrations/          # Telethon、Bot API 等外部系统适配器
└── schemas.py             # YAML/API 输入模型
```

根目录只保留包入口、配置和 schema；业务模块统一放在上述职责子包中。

## 依赖方向

`core` 不依赖任何基础设施；`features` 只依赖 `core` 和纯函数；
`integrations` 实现 `features` 所需的协议；`runtime`、`interfaces` 和 `cli`
负责组装对象。新增功能时优先添加 feature 与 integration，避免把业务判断继续
写入 CLI 或 Telethon 事件回调。

## Telegram 机器人管理

使用 `CommandRegistry` 注册显式命令。命令处理器接收标准化的
`CommandContext`，可以是同步或异步函数。Telegram 事件适配器只需要完成：

1. 将 Telethon 事件转换为 `CommandContext`；
2. 调用 `registry.dispatch(event.raw_text, chat_id=..., sender_id=...)`；
3. 将非空返回值发送回原聊天。

注册表不会执行任意 Python、Shell 或表达式；命令到处理器的映射必须由代码或
受校验的配置显式声明。

## 频道通知与群监控

`ChannelNotifier` 通过注入的 transport 发布 `ChannelNotification`，因此可以
切换 Bot API、用户账号或消息队列而不修改业务层。`GroupMonitor` 只负责规则匹配、
有限历史和可选总结器；收发 Telegram 消息应放在 integration 层。规则回复和总结
均为可替换依赖，便于增加限流、审核和持久化策略。

## 数据与运行时原则

- 所有持久化时间使用 UTC；展示时才转换为任务时区。
- 对外 API 和运行日志中的 UTC 时间统一使用秒精度 RFC 3339 格式（例如 `2026-08-31T13:16:45Z`）。
- `nextRunAt` 表示未来计划时间，`lastRunAt` 表示上一次运行完成时间，两者不应被视为同一运行时刻。
- 外部调用必须可取消、可重试，并通过依赖注入便于离线测试。
- 每项新能力都应有独立的配置开关；默认关闭，不改变签到服务行为。
- 不在日志中记录 Token、session、手机号、验证码和群消息原文等敏感数据。
