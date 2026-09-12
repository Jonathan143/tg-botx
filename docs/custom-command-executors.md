# 自定义命令执行器：设计、部署与前端对接

本次实现以主分支 `d100991f609d26d5dba7962d6ed137203e945218` 为基线，不依赖已关闭、未合并的 PR #7。补齐管理 Bot 自定义命令实际执行链路，移除 JavaScript 写入支持。本文中的接口均位于本仓库后端；独立前端仓库未在本次改动中修改。

## 1. 范围与架构决策

| 类型 | 职责 | 可执行前提 |
| --- | --- | --- |
| `none` | 未配置的自定义命令草稿 | 自定义命令不能启用；系统命令继续使用原有处理器 |
| `http` | 调用部署端明确允许的外部 API | 完整配置、精确 origin 白名单、实际连接地址安全 |
| `builtin_function` | 服务端显式注册的只读能力 | 函数/参数合法，命令角色与函数角色相交 |
| `python` | 隔离的数据处理脚本 | 部署开关、独立 Runner 健康、源码哈希确认 |
| `javascript` | 仅保留历史读取 | 已移除，不能创建、启用或更新脚本 |

设计拆为三个可独立理解的层次，但在本次功能分支一起提供：① 类型移除、历史迁移、状态与配置校验；② 统一调度、HTTP 与内置函数；③ 独立 Python Runner、限额与恢复。现有签到工作流仍只接受显式步骤，不增加任意 Python/Shell 节点。Python 例外仅限管理命令通过独立工作负载执行。

```text
Telegram polling/webhook → 私聊/身份/命令权限检查 → 数据库原子入队
管理员真实试运行       → 管理会话/CSRF/确认检查 → 相同数据库队列
数据库队列 → 固定 Worker → 重新检查配置版本/身份/运行能力
           → HTTP / 内置函数 / 独立 Python Runner
           → 持久化结果与审计 → 独立回复投递 Worker → Telegram 纯文本
```

`handlers.py` 只做协议转换与快速入队，不等待慢请求。`application/command_executors.py` 和应用容器统一装配资源；`CommandExecutionService.close()` 取消工作任务，再关闭 HTTP/Runner 客户端。内置函数只获得必要数据库能力；脚本不接收数据库、Bot Client、Token、配置对象或宿主环境。

主要模块：`features/bot/executors/` 定义协议、schemas、模板、能力注册与三种执行器；`integrations/safe_http.py` 管理受控出站；`integrations/python_runner.py` 是固定 Runner 协议适配；`runner/service.py` 是独立 Docker 工作负载管理端；`repositories/command_executions.py` 管理持久化入队、领取、完成和回复。

## 2. 迁移与兼容

数据库版本 8 增加命令 `revision` / `confirmed_code_hash`，并创建执行记录及每个 Bot 的事务锁表。迁移沿用 SQLite 写事务、PostgreSQL advisory lock，版本记录与迁移一起提交。

**升级前备份数据库。升级会停用并隐藏所有历史自定义命令，包括 HTTP、Python 和 JavaScript。** 原版本这些命令从未实际执行，不能在升级后突然产生外部副作用。原始 `executor_type`、`executor_config_json` 不删除、不自动改写为 `none`，JS 不自动转换为 Python。新配置校验通过后由管理员重新启用。

JS 显示 `retired`；其他未知字符串显示 `unsupported`；无效 JSON/配置显示 `invalid_config`。停用、改名、修改描述不会覆盖历史原始配置。更换执行器必须提交完整的新配置。系统命令及 `/task`→`/tasks` 保留别名不允许被自定义命令覆盖。

迁移只执行一次，后续重启不重复停用已确认命令。没有自动降级迁移；回滚请恢复升级前备份。已运行的外部副作用不能通过回滚数据库撤销。混合运行升级前/后版本会绕过新配置规则，不支持滚动混部；应先停止旧服务、迁移，再启动新版本。

## 3. 统一状态、参数与输出

命令列表和创建/更新响应保留原字段并增加：

```json
{
  "command": "py_example",
  "executorType": "python",
  "enabled": true,
  "executionStatus": "unavailable",
  "executionErrorCode": "EXECUTOR_UNAVAILABLE",
  "unavailableReason": "Python Runner 当前不可用",
  "effectiveEnabled": false,
  "effectiveAllowedRoles": ["user", "admin"],
  "revision": 2,
  "codeHash": "<sha256-of-code>",
  "codeConfirmed": true,
  "lastExecution": null
}
```

`enabled` 是管理员意图；`effectiveEnabled` 是当前是否可执行；`menuVisible` 仅控制展示，不授予权限。状态集合为 `ready`、`not_configured`、`invalid_config`、`retired`、`unsupported`、`blocked_by_policy`、`unavailable`。菜单推送和 `/help` 过滤不可执行命令；Telegram 菜单是缓存，不能替代运行时权限检查。保存后按现有前端流程调用 `/api/bot/commands/sync` 推送菜单；启动时也会刷新，隐藏迁移后停用项。

可调用角色仍是 `anonymous`、`user`、`admin`。为兼容旧接口，`allowedRoles: []` 仍表示全部角色，不表示禁止所有人；内置函数另外强制取权限交集。

模板只支持 `{{ argument }}`、`{{ command }}`、`{{ user.id }}`、`{{ user.role }}`、`{{ chat.id }}`。`argument` 为命令后完整原始参数字符串，不进行 Shell 分词。禁止表达式、函数调用、对象反射和未知变量；只渲染值，不渲染 JSON 键或 URL origin。管理员试运行没有 Telegram 用户/聊天 ID，使用对应身份模板或 `my_points` 会明确失败，不允许伪造用户。

统一结果：`{"text":"回复文本","data":null}`，可选 `data` 必须为 JSON 对象。最大 3500 UTF-16 单元（emoji 通常占两单元）、总 JSON 16KiB；禁止空文本、非有限数字、无效 Unicode、超深结构和未知输出字段。Telegram 使用无 `parse_mode` 的纯文本发送，避免 HTML 注入及切断实体。审计只写执行编号、命令、结果与错误码，不写源码、参数、响应正文或密钥。

## 4. HTTP 执行器

```json
{
  "command": "lookup",
  "description": "查询外部服务",
  "enabled": true,
  "allowedRoles": ["user", "admin"],
  "executorType": "http",
  "executorConfig": {
    "version": 1,
    "method": "GET",
    "url": "https://api.example.com/search",
    "query": {"keyword": "{{ argument }}", "userId": "{{ user.id }}"},
    "headers": {"Accept": "application/json"},
    "credentialRef": "lookup_service",
    "timeoutSeconds": 10,
    "response": {"type": "json", "textPath": "data.reply"}
  }
}
```

示例域名不代表真实服务，必须替换并由部署端加入白名单。支持 GET/POST/PUT/PATCH/DELETE/HEAD；GET/HEAD 不带请求体；`jsonBody` 与 `textBody` 互斥。JSON 按对象值渲染后序列化，query 由客户端编码，用户引号/斜杠不能破坏 JSON 或 URL 参数结构。`response.type=text` 返回 UTF-8 文本；`json` 按点分路径提取字符串，允许数组数字下标，如 `items.0.title`。第一版不提供表达式 JSONPath、请求链或循环。HEAD 无正文通常不能生成有效回复，因此会返回响应无效错误。

部署配置（仅占位示例）：

```dotenv
TG_BOT_COMMAND_HTTP_ALLOWED_ORIGINS='["https://api.example.com"]'
TG_BOT_COMMAND_HTTP_CREDENTIALS='{"lookup_service":{"origin":"https://api.example.com","headers":{"Authorization":"Bearer <secret-from-deployment>"}}}'
```

白名单是规范化后的 **scheme + hostname + port 精确匹配**，不支持通配符。默认空白名单，即 HTTP 禁用；不提供命令级“允许私网”绕过开关。凭据必须绑定 HTTPS origin，API 只列引用名和 origin，不返回密钥。禁止直接配置 Authorization/Cookie/API-key、Host、Content-Length、Transfer-Encoding 等敏感/协议头。不要把密钥放在 URL/query/代码中；这类字符串会作为管理员配置持久化，并非密钥管理接口。

出站使用独立 `SafeHttpClient`：

- 仅 HTTP(S)，拒绝 URL 凭据、fragment、控制字符、动态主机、端口或协议。精确 origin 校验后，**在每次 TCP 连接时**解析全部 DNS 结果，任何私网/回环/保留/链路本地/云元数据/过渡 IPv6 地址都会阻断整次连接。
- 只把已校验的数字 IP 交给底层 socket；原主机名保留用于 TLS SNI 和证书校验。禁用连接复用，下一次连接重新检查，避免“预检一次、客户端再次解析”的重绑定缺口。
- 不跟随任何重定向，不读取环境代理，不自动重试。凭据不会随跨 origin 请求发送。网络层固定 HTTP/1.1，使用 httpcore 的公开 network-backend 接口，没有关闭证书校验。
- 默认总时限 10 秒，可配置 1–30 秒，包含 DNS/连接/响应读取；请求体 32KiB，原始和解压后响应分别限制 64KiB，流式读取并及时关闭。限制 gzip/deflate 解压量，拒绝未知编码和畸形响应。
- 系统 DNS 线程不能被 Python 强制终止，因此另设 4 个未完成解析槽位；超时不提前释放未结束的系统解析槽，防止无限堆积线程。

非 2xx、字段缺失、非字符串结果、解码错误均返回稳定错误码。不默认重试 POST 等操作；**超时不代表上游未执行**。外部恰好一次只能由上游业务幂等协议共同保证。本次不修改已有签到工作流 HTTP 步骤的行为；后续可复用 `SafeHttpClient`，但必须明确迁移其网络策略。

## 5. 内置函数执行器

```json
{
  "executorType": "builtin_function",
  "executorConfig": {
    "version": 1,
    "function": "echo",
    "arguments": {"text": "{{ argument }}"}
  }
}
```

| 函数 | 参数 | 函数最低权限 |
| --- | --- | --- |
| `echo` | `text: string` | 所有角色，仍受命令权限限制 |
| `utc_time` | 空对象 | 所有角色，仍受命令权限限制 |
| `my_points` | 空对象 | 已绑定用户，只查本人 |
| `system_status` | 空对象 | 管理员，仅任务总数/启用数 |

仅显式注册四种只读函数，按各自 Pydantic schema 验证参数。禁止动态导入、字符串函数路径、`eval` 和任意 `getattr`。真实权限是命令角色 ∩ 函数角色 ∩ 业务资源权限；不接受参数中的 userId/role 作为身份。积分修改、删除任务、触发任务等写能力不在本版范围，应另做确认、幂等和事务设计。

## 6. Python Runner 与部署

脚本入口：

```python
def main(ctx):
    text = ctx["argument"].strip()
    return {"text": text.upper(), "data": {"length": len(text)}}
```

配置为 `{version: 1, code: "源码", timeoutSeconds: 3}`。源码必须有唯一同步 `main(ctx)`；AST 检查仅验证语法和入口，**不是安全沙箱**。代码在独立容器中是普通 Python，能访问容器内部标准库和文件；不声称“限制 import 就安全”。外部网络/业务资源分别走 HTTP/内置函数，不从脚本直接开放。

### 部署端

先在专用 Linux Runner 主机安装 Docker Engine（支持 memory/pids/CPU/seccomp），构建固定工作负载镜像并安装此版本项目：

```bash
docker build -f runner/Dockerfile -t tg-botx-python-sandbox:1 .
python -m pip install .
export TG_BOT_RUNNER_TOKEN='<至少32字节的独立随机密钥>'
export TG_BOT_RUNNER_IMAGE='tg-botx-python-sandbox:1'
export TG_BOT_RUNNER_INSTANCE='production'
export TG_BOT_RUNNER_HOST='127.0.0.1'
export TG_BOT_RUNNER_PORT='8766'
python -m tg_botx.runner
```

占位密钥不能用于生产；建议 `openssl rand -hex 32` 生成。Runner 不加载项目 `.env`，只读取 `TG_BOT_RUNNER_*`。默认 Unix socket 为 `unix:///var/run/docker.sock`；rootless Docker 可用 `TG_BOT_RUNNER_DOCKER_HOST=unix:///run/user/<uid>/docker.sock`，仍须通过资源控制探测。默认并发 2（`TG_BOT_RUNNER_CONCURRENCY`）。同一 daemon 上每个 Runner 的 INSTANCE 必须唯一，且每实例只运行一个管理进程，避免启动清扫互相影响。

独立机器部署使用 HTTPS：设置 `TG_BOT_RUNNER_TLS_CERTFILE`、`TG_BOT_RUNNER_TLS_KEYFILE` 及监听地址；限制网络访问者。远程 URL 强制 HTTPS且验证证书；数字 loopback 地址才允许 HTTP。同主机测试可用 loopback，但正式多租户建议独立主机及更强隔离。Bot 容器不挂 Docker socket；需要访问宿主机 Runner 时使用受保护 HTTPS 服务地址，不能用 `http://host.docker.internal` 绕过加密检查。

主服务设置：

```dotenv
TG_BOT_COMMAND_PYTHON_ENABLED=true
TG_BOT_COMMAND_PYTHON_RUNNER_URL=https://runner.example.com
TG_BOT_COMMAND_PYTHON_RUNNER_TOKEN=<与Runner一致的随机密钥>
```

Runner 探测本地镜像并固定不可变 image ID，不自动拉取任意镜像。生产构建还应固定基础镜像 digest、更新安全补丁，确保宿主机内核/容器运行时受维护。

每次执行新建工作负载：非 root 65534、`--network none`、只读根、cap-drop ALL、no-new-privileges、默认 seccomp、128MiB 内存及相同 memory-swap、0.5 CPU、pids 16、nofile/fsize/CPU 上限、8MiB noexec 临时目录、禁用 Docker 日志、init 回收子进程。不挂载应用目录、DB、`.env`、session 或 Docker socket，不继承宿主环境。只有固定镜像、固定启动参数；命令不能选择镜像/挂载/网络。

脚本默认时限 3 秒（1–10），包含容器 attach/start；stdout、stderr 各 16KiB，协议输入 32KiB。返回结果单独 JSON，普通 print 重定向到 stderr 并限量，不回传日志。超时/取消终止整个容器，独立管理端截止时间不依赖调用者是否在线。生命周期维护每 5 秒清扫过期容器；启动清扫所属 INSTANCE 遗留容器。清理状态不明确则隔离该管理实例、拒绝新执行，直到确认清理完成；不能把 Docker daemon 不可达误判为容器已不存在。

Docker 控制接口权限很高，不能开放给脚本或互联网。普通加固容器共享宿主内核，不是绝对隔离保证；面对不可信多租户需另评估 gVisor/microVM/专用主机。Runner 的健康探测与源码确认不是安全证明，测试亦不能证明不存在容器逃逸。

### 源码启用确认

调用纯校验接口取得 `codeHash`。管理员确认源代码后，创建/更新请求提交相同 `confirmCodeHash` 及 `enabled=true`。部署开关、Runner 健康和确认三者缺一不可；没有无隔离回退模式。源码任何字节变化都会清除确认，未显式提交 enabled 时自动停用；明确要求启用但没有新哈希则拒绝整个事务。对源码做格式化后也必须重新确认。

### Runner 固定协议

全部接口要求 `Authorization: Bearer <runner-token>`：GET `/v1/health` 返回 ready/protocolVersion=1/isolated；POST `/v1/executions` 接受 `executionId`（UUID）、`code`、`context`、`timeoutSeconds`；DELETE `/v1/executions/{id}` 取消。协议不接受镜像、路径、环境变量、外部权限。服务端限制请求体并对参数错误脱敏。重复/已取消 ID 在当前实例内有界拒绝；跨重启持久去重由 Bot 数据库保证。

## 7. 持久化调度、错误和运行记录

默认并发：全局 8、Python 2、每个 actor 1；待执行+运行中总数最多 100；排队 60 秒；每 actor 每分钟 5 次；运行资料保留 30 天。部署变量为 `TG_BOT_COMMAND_MAX_WORKERS`、`...PYTHON_WORKERS`、`...QUEUE_LIMIT`、`...QUEUE_SECONDS`、`...RATE_LIMIT`、`...RETENTION_DAYS`。管理员试运行共享 `admin-api` actor，不因换会话绕过限额。

每 Bot 锁行在 PostgreSQL 用 FOR UPDATE，SQLite 用 BEGIN IMMEDIATE；原子完成配额检查和任务插入/领取。数据库唯一键 `(bot_identity,dedupe_key)` 对 Telegram update 持久去重，重复请求不占配额。不同 Bot 分开去重；同 Bot 多个 Worker/进程共享配额，但部署参数应一致；Telegram polling 本身仍只应运行一个接收实例。

领取时保存 owner/lease，真正执行前重新读取命令 revision 和绑定角色。排队期间配置变动或权限撤销会阻止执行。排队过期 → cancelled；执行超时/失败 → failed；工作进程崩溃或关闭导致结果不确定 → unknown，**不重新执行**。崩溃 lease 恢复窗口为 90 秒；已排队未运行记录在 TTL 内可以由健康进程领取。

执行状态与交付状态分离。结果/审计提交后由另一组 Worker 发送，发送失败最多重试 3 次，不再次运行执行器；进程崩溃后也仅恢复交付。Telegram 自身不提供这里需要的幂等发送协议，因此崩溃窗口可能重复显示回复，但不会因此重做业务。尚未确认持久入队的存储错误不会被 Webhook 当成功吞掉，polling offset 也会退回该 update 等待重试。

查询接口只返回执行摘要/结果，不返回原始参数或配置。数据库内部保存参数和配置快照，可能包含个人数据；应限制 DB/备份访问、磁盘加密，并配置合适保留期。终态执行记录按保留期清理后，对应去重窗口也结束，不能宣称永久去重；既有审计日志使用原项目保留机制。

稳定错误码包括：`INVALID_EXECUTOR_CONFIG`、`EXECUTOR_RETIRED`、`EXECUTOR_UNAVAILABLE`、`EXECUTOR_NOT_CONFIGURED`、`EXECUTION_FORBIDDEN`、`CONFIG_CHANGED`、`HTTP_TARGET_BLOCKED`、`HTTP_REQUEST_FAILED`、`HTTP_RESPONSE_INVALID`、`EXECUTION_TIMEOUT`、`OUTPUT_TOO_LARGE`、`EXECUTION_FAILED`、`EXECUTION_BUSY`、`EXECUTION_RATE_LIMITED`、`QUEUE_EXPIRED`、`EXECUTION_INTERRUPTED`、`SERVICE_STOPPING`、`MISSING_TEMPLATE_VARIABLE`、`IDEMPOTENCY_CONFLICT`。用户回复为脱敏说明+执行编号，管理员用编号查详情。

## 8. 管理 API / 前端对接

沿用管理会话 Cookie、Origin、JSON Content-Type、`X-CSRF-Token`，所有新接口继承认证；命令写请求体限制 64KiB，executorConfig 32KiB，参数 UTF-8 4KiB。请求模型 extra=forbid。

| 接口 | 行为 |
| --- | --- |
| GET `/api/bot/executors` | 三种类型及 JSON Schema、部署可用性、模板变量、origin/凭据引用、限额 |
| GET `/api/bot/builtin-functions` | 四种函数、参数 Schema、角色和无副作用说明 |
| POST `/api/bot/commands` | 创建；支持 executorType/executorConfig/confirmCodeHash |
| PATCH/PUT `/api/bot/commands/{command}` | 合并实际出现字段后原子校验+保存，支持 expectedRevision |
| POST `/api/bot/command-validation` | 纯校验，无网络、无脚本执行；运行可用性使用最近缓存 |
| POST `/api/bot/command-tests` | 真实管理员试运行，202 返回 executionId；正式执行同链路 |
| GET `/api/bot/command-executions` | 最近执行，支持 command、limit（1–100），默认 50 |
| GET `/api/bot/command-executions/{id}` | 本 Bot 的执行状态/结果/交付状态，不存在返回 404 |

PATCH 中未出现字段保留原值；不接受显式 null。更换类型需完整 executorConfig，不能混入旧类型字段。expectedRevision 冲突返回 409；校验失败不会先改名或留下部分更新。仅缺少必填字段的配置可作为停用草稿保存，未知字段/错误类型即便停用也拒绝。

纯校验请求与响应：

```json
{"executorType":"builtin_function","executorConfig":{"function":"echo","arguments":{"text":"{{ argument }}"}}}
```

返回 valid（结构是否完整）、canEnable（结构+策略+确认+缓存健康）、errors、规范化 executorConfig、executionStatus、unavailableReason、codeHash。Python 首次验证 valid=true 但未确认时 canEnable=false 是正常结果；提交 hash 后才满足确认条件。

真实试运行请求示例：

```json
{
  "executorType": "builtin_function",
  "executorConfig": {"function":"echo","arguments":{"text":"{{ argument }}"}},
  "argument": "Hello"
}
```

HTTP/Python 必须额外 `confirmExecution: true`；Python 同时需要 `confirmCodeHash`。不能把试运行标成无副作用预览，也不能伪造 userId/role。请求头支持 `Idempotency-Key`（最长 128 字符），相同 key+请求返回同一执行号；相同 key 不同请求 409。未提供 key 每次视为新执行。收到 202 后轮询详情（建议 0.5–1 秒一次，到终态停止）；不要因为网络超时自动用新 key 重试有副作用请求。

前端类型和状态处理建议：

```ts
type WritableExecutor = 'none' | 'http' | 'builtin_function' | 'python';
type ExecutionStatus = 'ready' | 'not_configured' | 'invalid_config' | 'retired'
  | 'unsupported' | 'blocked_by_policy' | 'unavailable';
interface CommandExecutionState {
  executorType: string; // 读取必须容忍 javascript 和未来/未知的历史类型
  executionStatus: ExecutionStatus;
  enabled: boolean;
  effectiveEnabled: boolean;
  unavailableReason: string | null;
  revision: number;
  codeHash: string | null;
  codeConfirmed: boolean;
}
```

创建选择器不再显示 JS；旧记录仍显示“已移除”，允许查看、复制、更换和删除。表单先按能力接口渲染，再取 builtin 参数 schema。展示管理员启用意图与实际状态，不把 disabled/unavailable 混成成功。Python 编辑器每次源码变化清空本地确认，提交前重新校验并人工确认。表单将源码交给 JSON.stringify 序列化，不手工拼接换行 JSON。保存后使用返回的新 revision，并按现有菜单流程 sync；并发冲突提示重新加载。

## 9. 验收与限制

专项测试位于 `tests/executors/`：类型/历史数据迁移、真实列升级、未知配置读取、原子 rename/更新/并发创建、JS 拒绝、系统 none 回归、Python 哈希失效、JSON/UTF-16 限额、模板白名单、真实 httpcore 底层连接 IP+TLS SNI、混合 DNS/重绑定/重定向/解压炸弹、凭据绑定、并发/去重/配额/lease恢复、配置变动、独立回复交付、API 鉴权/CSRF/限流/幂等，以及 Runner 固定协议和清理失败隔离。

CI 在 Python 3.12/3.13 运行质量检查和专项测试，在 PostgreSQL 服务上重跑迁移/并发事务测试，并构建真实 Linux Docker 镜像验证正常执行、非 root/只读/无外网/无宿主凭据、超时、无限输出、内存限制和子进程清理。Docker 测试只在 `RUN_EXECUTOR_DOCKER_TESTS=1` 时运行；未设置时明确 skipped，不能把模拟测试当真实容器验收。

没有发送真实 Telegram 消息、调用真实业务接口、修改部署配置或验证生产数据库备份升级。主分支已有的无关测试失败不在此功能中顺带修复；PR 描述记录基线与本分支对比。容器安全、外部服务幂等与生产 TLS/防火墙仍需部署端验收。

参考：HTTPcore Network backends（https://www.encode.io/httpcore/network-backends/）；Docker resource constraints（https://docs.docker.com/engine/containers/resource_constraints/）；Docker Engine security（https://docs.docker.com/engine/security/）；OWASP SSRF Prevention（https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html）。
