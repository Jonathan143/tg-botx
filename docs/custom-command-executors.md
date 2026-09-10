# 自定义指令执行器对接说明

管理 Bot 的自定义指令通过 `/api/bot/commands` 创建，通过 Telegram 私聊调用。创建接口仍然允许先保存停用的 `none` 草稿；只有配置了可执行执行器后，才能把 `enabled` 改为 `true`。创建或更新时遇到不合法配置会返回统一的 `VALIDATION_FAILED`（HTTP 422），不会保存半可用配置。

## 创建和更新

请求字段使用前端的 camelCase 名称：

```json
{
  "command": "weather",
  "description": "查询天气",
  "enabled": true,
  "menuVisible": true,
  "allowedRoles": ["user", "admin"],
  "executorType": "http",
  "executorConfig": {
    "url": "https://api.example.com/weather?city={{arg0}}",
    "method": "GET",
    "timeoutSeconds": 10,
    "retries": 1,
    "responseFormat": "json",
    "responsePath": "data.summary",
    "allowedHosts": ["api.example.com"]
  }
}
```

编辑接口 `PATCH /api/bot/commands/{command}` 支持同样的 `executorType` 和 `executorConfig` 字段。字段省略时保留原执行器配置；传入空对象只会在执行器允许的情况下清空配置。系统内置指令不能配置自定义执行器。

`GET /api/bot/commands` 返回的 `executorType` 和 `executorConfig` 可直接回填编辑表单。建议前端根据执行器类型渲染配置表单，不要让用户直接编辑任意 JSON。

## 命令变量

HTTP URL、请求头、请求体字符串和 builtin 的 `template` 支持以下变量：

| 变量 | 含义 |
| --- | --- |
| `{{command}}` | 不带 `/` 的命令名称 |
| `{{args}}` | 命令后的原始参数字符串 |
| `{{arg0}}`、`{{arg1}}` | 使用 Telegram shell-like 引号拆分后的参数 |
| `{{argsJson}}` | 参数数组 JSON 字符串 |
| `{{text}}` | 完整 Telegram 文本 |
| `{{userId}}` | Telegram 用户 ID |
| `{{chatId}}` | Telegram 私聊 ID |

未知变量会在执行时返回明确错误。模板值只做字符串替换，不执行表达式。

## 执行器配置

### `http`

- `url` 必填；仅支持 `https`，使用明文 `http` 时必须显式设置 `allowInsecureHttp: true`。
- `allowedHosts` 可选；填写后目标主机必须精确匹配列表；当 url 主机名包含模板变量时必须显式配置以防范 SSRF。
- 禁止 URL 用户名/密码、`Host` 请求头和重定向；目标 DNS 解析到内网、回环、链路本地、保留或非全球 IP 时会拒绝请求。
- `method` 支持 `GET`、`POST`、`PUT`、`PATCH`、`DELETE`、`HEAD`。
- `timeoutSeconds` 范围为 1–30，`retries` 范围为 0–3。网络错误和 408/429/5xx 响应才会重试。
- `body` 必须是 JSON 值；`responseFormat` 为 `text` 或 `json`，JSON 响应可用 `responsePath` 取点分隔字段。
- 单次响应最多 64KB，返回给 Telegram 的文本最多 12KB。

### `builtin_function`

只能选择服务端显式注册的函数，当前函数名为：

- `args`：返回原始参数；
- `echo`：返回 `template`（默认 `{{args}}`）；
- `json`：返回完整命令上下文 JSON；
- `utc_time`：返回当前 UTC ISO 时间。

未知函数不会被保存，服务端不会从数据库导入 Python callable。

### `python` 和 `javascript`

脚本执行器默认关闭。要启用必须同时满足：

```json
{
  "executorType": "python",
  "executorConfig": {
    "code": "print(context['args'][0] if context['args'] else 'empty')",
    "allowExecution": true,
    "timeoutSeconds": 2
  }
}
```

Python 脚本通过 `context` 读取上下文并通过 `print` 返回结果；JavaScript 脚本同样使用 `context`，通过 `console.log` 返回结果。脚本不能导入模块或访问文件、网络、进程 API，运行在一次性子进程、临时工作目录和资源限制下，超时或非零退出会返回执行失败。Python 代码最多 16KB、运行时间最多 5 秒；JavaScript 需要服务端安装 Node.js。

对于不需要脚本的场景，优先使用 `http` 或 `builtin_function`。前端应在启用脚本执行器前展示风险提示，并将 `allowExecution` 明确显示为二次确认项。

## Telegram 调用和错误

```text
/weather Tokyo
```

执行成功后返回执行器输出；空输出返回“指令执行成功，但没有返回内容”。配置错误、HTTP 目标限制、超时和脚本失败都会返回带原因的失败消息，并写入管理 Bot 审计日志。执行器输出会按 Telegram HTML 消息规则进行转义，远程响应不能注入 Telegram 标记。

