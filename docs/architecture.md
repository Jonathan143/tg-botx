# 架构与扩展约定

项目采用 `src` 布局。业务操作通过共用服务完成，入口负责输入、输出和协议转换；网络适配、持久化与运行资源分别管理。

```text
src/tg_botx/
├── application/
│   ├── container.py            # CLI/API 共用组装、资源所有权和关闭
│   └── queries.py              # 批量装配任务读模型
├── interfaces/
│   ├── cli.py                  # Typer 命令
│   ├── account_console.py      # 终端输入、二维码展示
│   ├── admin/
│   │   ├── admin_api.py        # 应用工厂与路由注册
│   │   ├── routes/             # 任务、运行、账号、机器人、认证、日志等路由
│   │   ├── security/           # 密钥、会话、限流与代理地址处理
│   │   ├── models.py           # HTTP 请求模型
│   │   ├── presenters.py       # 响应转换及统一进度脱敏
│   │   ├── middleware.py       # 鉴权和错误转换
│   │   └── lifecycle.py        # 进程信号、启动与关闭
│   └── telegram/
│       ├── runtime.py          # webhook、轮询和菜单同步
│       └── handlers.py         # 消息、回调、任务操作和展示
├── features/
│   ├── accounts/
│   │   ├── service.py          # 登录状态机和兼容服务入口
│   │   ├── directory.py        # 账号查询与退出规则
│   │   ├── access.py           # 客户端租借与账号查找
│   │   ├── chats.py            # 聊天同步
│   │   ├── avatars.py          # 头像缓存和后台下载
│   │   ├── probe.py            # 消息探测
│   │   └── models.py           # 账号状态和视图
│   ├── bot/
│   │   ├── management.py       # 绑定、权限和共用管理入口
│   │   ├── commands.py         # 命令配置
│   │   ├── points.py           # 签到积分
│   │   └── models.py           # 角色、执行器类型和配置常量
│   └── checkin/
│       ├── runtime.py          # 服务门面、组件连接和生命周期
│       ├── coordinator.py      # 运行预占、串行锁、取消、快照和收尾
│       ├── tasks.py            # 创建、编辑、启停、归档
│       ├── workflows.py        # 发布版本
│       ├── scheduler.py        # 调度注册与同步
│       ├── schedule.py         # 固定/随机时间计算
│       ├── progress.py         # 运行状态、日志和合并更新订阅
│       ├── notifications.py    # 管理员通知
│       ├── executor.py         # 步骤编排、状态报告和重试
│       ├── execution_types.py  # 执行上下文与步骤状态
│       ├── step_models.py      # 步骤字段模型及允许字段
│       ├── validation.py       # 跨节点引用和变量语义校验
│       ├── steps/              # 六种显式允许的步骤处理器
│       └── conditions/         # 值转换、正则预算、变量和条件求值
├── infrastructure/
│   ├── persistence/
│   │   ├── db.py               # 数据库连接与兼容访问门面
│   │   ├── models.py           # ORM 模型与 UTC 字段类型
│   │   ├── migrations.py       # 有版本的增量迁移
│   │   └── repositories/       # 账号、任务、运行、机器人、会话、统计
│   └── observability/
│       ├── logging.py          # 日志格式与脱敏
│       └── log_stream.py       # 共享增量尾读和订阅
├── integrations/
│   ├── telegram.py            # Telethon 客户端与 session 构造
│   ├── client_pool.py         # 账号客户端租借与连接复用
│   ├── checkin_messages.py    # Telegram 消息等待、元数据及按钮适配
│   └── telegram_bot.py        # Telegram Bot API
├── core/                      # 时间、Clock 协议和可选扩展契约
├── config.py                  # 环境配置
└── schemas.py                 # 稳定的任务 YAML/API 模型入口
```

## 依赖和资源边界

`application/container.py` 是默认组装入口。`build_context()` 创建数据库、客户端池、通知和调度器；`build_admin_context()` 装配登录、管理机器人、安全组件和日志订阅。外部传入的数据库不会由应用上下文擅自销毁；上下文自己创建的数据库会在关闭时释放连接池。

`CheckinService` 保留现有接口，并委托给任务、版本、进度、调度和运行协调组件。组件共享同一组运行状态、预占集合和版本计数，不能自行创建另一组运行锁。运行协调和调度使用可注入的 `Clock`；客户端池、通知和调度器也可替换。

当前采用务实的服务与 Repository 分层，服务仍使用 ORM 记录作为内部数据对象，并未声称实现完全独立于 SQLAlchemy 的领域模型。数据库写入封装在 Repository 中；入口和账号服务不自行提交数据库会话。新能力应依赖所需的最小接口，避免把现有兼容门面扩展成新的业务实现文件。

## 业务一致性和事务

CLI 与 HTTP 共用任务创建、启停和发布规则。创建的任务默认停用且没有下次执行时间；归档任务仍占用名称。登录和退出共用账号状态机与规则，终端输入及二维码输出仅存在于 CLI 适配器中。

工作流版本和已发布调度配置在同一数据库事务中提交。PostgreSQL 发布时锁定任务行；SQLite 保持现有写事务与唯一约束。事务完成后才同步进程内调度并发布任务更新。进程内调度同步失败不会回滚已提交的版本，服务重启会从数据库重建调度。

## 工作流扩展

新增步骤时，在 `step_models.py` 定义字段，在 `steps/` 添加处理器，并显式注册到执行器的允许列表。字段集合来自步骤模型；变量可用性、条件分支、嵌套深度及数据源引用继续由语义校验器负责。

存储和对外传输仍使用原有字典/YAML 格式，不自动补入步骤默认字段。旧条件格式仍在执行边界规范化。条件正则继续保留超时、输入长度和总预算限制。不得增加任意代码、Shell 或不受控表达式执行能力。

HTTP 步骤在同一次执行中复用客户端，执行结束后关闭自有客户端；调用方传入的客户端仍归调用方管理。步骤循环统一处理取消、计时、失败报告和分支状态，处理器只执行对应能力。

## 查询和日志

任务列表一次性批量读取账号、版本和运行标记；`TaskQueries` 装配读模型，`task_json()` 只进行转换。运行列表批量读取所属任务。为兼容现有前端，列表响应字段仍保留；未自行删除工作流版本等字段。

进度脱敏使用副本，避免修改运行时或持久化原始进度。UTC 对外时间继续使用秒精度 RFC 3339；通知展示按指定时区格式化。

日志 SSE 订阅共享一个后台读取任务，按文件 inode 和偏移读取新增内容，保留未完成行并处理文件轮转和可观察到的截断。文件读取在线程中执行。没有订阅者时停止读取；订阅队列有容量限制，消费过慢时发送 `gap` 事件，客户端可重新加载日志列表。历史日志查询与下载仍提供原有能力。

## 迁移和兼容

新表由 ORM 元数据创建，已有表按 `migrations.py` 中的版本顺序升级。旧版 `schema_version=2` 数据库从版本 3 继续处理角色、运行快照、头像版本、已发布调度和命令配置。迁移和版本记录一起提交；重复启动不重复执行已完成的升级。SQLite 使用写事务，PostgreSQL 使用事务级 advisory lock 串行化迁移。

升级前应备份业务数据库；本次没有实现自动降级迁移。生产 PostgreSQL 和历史数据库备份的升级验证仍需在对应环境中完成。

旧的 `features.admin_bot`、`features.accounts.auth`、`interfaces.admin.admin_accounts`、`interfaces.admin.admin_security`、`features.checkin.condition` 和数据库门面继续保留兼容导出。新代码使用实际职责模块。

`EventBus`、`CommandRegistry`、`ChannelNotifier`、`GroupMonitor` 保留为独立扩展能力，没有自动接入当前管理机器人。它们的语义与任务进度的合并订阅不同，不能直接替换。配置中的扩展开关不代表已经完成运行链路接入；新增接入需同时提供实际 transport、生命周期与配置说明。
