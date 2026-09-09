# 管理 Bot 自定义指令前端对接

## 执行器

创建或更新自定义指令时，`executorType` 支持：

| 类型 | 配置 | 说明 |
|---|---|---|
| `http` | `{ "url": "https://example.com/hook", "method": "POST", "headers": {}, "timeout": 10, "retries": 0 }` | 服务端以 `{ "argument": "..." }` JSON 请求目标地址，响应正文作为回复；超时 1–30 秒，重试 0–3 次 |
| `builtin_function` | `{ "name": "echo" }` 或 `{ "name": "uppercase" }` | 仅允许白名单函数 |
| `python` / `javascript` | `{}` | 当前不接受配置，接口返回 422；待隔离运行环境上线后再开放 |
| `none` | `{}` | 未配置执行器，执行时返回错误 |

HTTP 仅允许 `http`/`https`，禁止用户名密码、内网/本机/保留地址和自动跳转；服务端会在执行前再次解析域名，避免把请求转发到内网。响应会按 Telegram 单条消息上限截断，敏感 header 不会写入日志。

## 接口

`GET /api/bot/commands` 返回 `{ "commands": [...] }`。自定义指令字段包括 `command`、`description`、`enabled`、`menuVisible`、`allowedRoles`、`executorType`、`executorConfig` 和 `updatedAt`。

创建请求：

```json
{
  "command": "report",
  "description": "生成报告",
  "enabled": true,
  "executorType": "http",
  "executorConfig": {"url": "https://example.com/hook", "method": "POST"}
}
```

`POST /api/bot/commands` 成功返回 `201`；不支持的类型、非法 URL、方法、超时、重试次数或 header 返回 `422 VALIDATION_FAILED`。启用 `none` 会返回同样的校验错误。执行器调用失败时，Bot 回复明确错误，不会暴露内部堆栈。

使用 `PATCH /api/bot/commands/{command}` 修改启用状态，也可同时传 `executorType`/`executorConfig` 更换执行器；使用 `DELETE` 删除自定义指令。系统指令不可删除或配置自定义执行器。修改菜单后调用 `POST /api/bot/commands/sync` 同步 Telegram 菜单。

历史上已保存的 Python/JavaScript 或无效配置会在 `GET /api/bot/commands` 中返回 `enabled: false` 和 `executorError`，不会执行。前端可提示管理员替换为受支持的执行器或删除该指令；将这类指令保持禁用状态的更新请求仍然允许提交，便于迁移旧数据。
