# 管理 Bot 自定义指令前端对接

## 执行器

创建或更新自定义指令时，`executorType` 支持：

| 类型 | 配置 | 说明 |
|---|---|---|
| `http` | `{ "url": "https://example.com/hook", "method": "POST", "headers": {}, "timeout": 10 }` | 服务端以 `{ "argument": "..." }` JSON 请求目标地址，响应正文作为回复，超时 1–30 秒 |
| `builtin_function` | `{ "name": "echo" }` 或 `{ "name": "uppercase" }` | 仅允许白名单函数 |
| `python` / `javascript` | `{}` | 默认禁用，接口返回 422，待隔离运行环境上线后再开放 |
| `none` | `{}` | 未配置执行器，执行时返回错误 |

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

`POST /api/bot/commands` 成功返回 `201`；不支持的类型、非法 URL、方法或超时返回 `422 VALIDATION_FAILED`。执行器调用失败时，Bot 回复明确错误，不会暴露内部堆栈。

使用 `PATCH /api/bot/commands/{command}` 修改启用状态，使用 `DELETE` 删除自定义指令；系统指令不可删除。修改菜单后调用 `POST /api/bot/commands/sync` 同步 Telegram 菜单。
