#
# AIBuilder Python Backend — HTTP / WebSocket 接口总览
#
# 路由来源：
# - `main.py`：`/health`、`/maintenance/cleanup`
# - `routers/tasks.py`：`/tasks/*`
# - `routers/conversations.py`：`/conversations/*`
#
# 数据模型见 `models.py`。
#

## 接口索引

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/maintenance/cleanup` | 手动触发过期 conversation 清理 |
| POST | `/tasks/generate-app` | 同步生成应用（阻塞至完成） |
| POST | `/tasks/generate-app-async` | 异步启动生成任务 |
| WebSocket | `/tasks/ws/{task_id}` | 任务过程事件推送 |
| POST | `/conversations` | 创建对话窗口 |
| GET | `/conversations` | 列出当前会话下的对话 |
| GET | `/conversations/{conversationId}` | 获取对话详情 |
| GET | `/conversations/{conversationId}/messages` | 获取聊天气泡历史 |
| POST | `/conversations/{conversationId}/messages` | 发送用户消息并启动一次 run |
| WebSocket | `/conversations/ws/{conversationId}/{runId}` | 单次 run 的过程事件推送 |

---

## `GET /health`

用途：简单健康检查，供前端或运维探活。

成功响应（200）：
- `status`：`"ok"`
- `workspaceRoot`：string（配置中的工作区根目录绝对路径字符串）

---

## `POST /maintenance/cleanup`

用途：手动触发过期 conversation 清理（删除数据库记录及对应生成目录）。行为与启动时可选清理一致，见 `config.cleanup_on_startup` / `conversation_ttl_days`。

请求体：无

成功响应（200）：
- `status`：`"ok"`
- `deleted`：number（本次删除的 conversation 数量）

失败响应（500）：
- JSON：`{ "error": "<异常信息字符串>" }`

---

## Tasks（`routers/tasks.py`）

---

## `POST /tasks/generate-app`

用途：同步方式生成应用（会阻塞直到任务完成），返回完整结果与过程事件列表。

请求体（JSON，`GenerateAppRequest`）：
- `description`：string，必填，最小长度 1
- `framework`：string，选填；默认 `expo`

成功响应（200，`GenerateAppResponse`）：
- `status`：`"completed"`
- `description`：string
- `framework`：string
- `logs`：string 数组（每轮对话与工具调用日志）
- `summary`：string（LLM 最终总结）
- `taskId`：string（任务 id）
- `expoRoot`：string（生成的 Expo 根目录）
- `expoUrl`：string 或 `null`（从日志提取的可访问 URL，前端用于二维码）
- `events`：`AgentEvent` 数组（过程事件，按时间线展示）

`AgentEvent`（`events` 内的对象）字段：
- `stepId`：int
- `type`：下列之一：`"round_start"`, `"llm_response"`, `"tool_call"`, `"tool_result"`, `"finished"`, `"command_start"`, `"command_output"`, `"command_end"`, `"expo_url_ready"`
- `title`：string
- `detail`：string 或 `null`

错误响应（400）：
- 返回 JSON：`{ "error": "description is required" }`

---

## `POST /tasks/generate-app-async`

用途：异步启动应用生成任务，立即返回 `taskId` 与 `expoRoot`；通过 WebSocket 获取实时进度。

请求体（JSON，`GenerateAppRequest`）：
- `description`：string，必填，最小长度 1
- `framework`：string，选填；默认 `expo`

响应（200，`StartGenerateAppResponse`）：
- `status`：`"accepted"`
- `taskId`：string（任务 id）
- `expoRoot`：string（生成的 Expo 根目录，便于前端在任务未完成前就知道路径）

错误响应（400）：
- 返回 JSON：`{ "error": "description is required" }`

---

## `WebSocket /tasks/ws/{task_id}`

用途：基于 `task_id` 实时推送任务过程事件；任务完成后推送一次最终状态并关闭连接。

连接阶段：
- 服务端在收到连接后会 `accept`，随后循环读取任务状态（`task_manager.get_state(task_id)`）。
- 若任务尚未初始化，会短暂等待后继续轮询。

消息推送：
过程事件（来自 `AgentEvent`，按新产生的事件逐条发送）：
- `stepId`：int
- `type`：见 `POST /tasks/generate-app` 的 `AgentEvent.type` 列表
- `title`：string
- `detail`：string 或 `null`

结束消息（当任务状态为 `completed` 或 `failed` 且没有新事件时）：
- 推送 JSON：
- `type`：`"task_status"`
- `status`：`"completed"` 或 `"failed"`
- `error`：string 或 `null`（仅 `failed` 时通常包含错误信息）

断开：
- 客户端主动断开时，服务端直接结束该连接的处理循环。

---

## Conversation（窗口/多轮聊天）

说明：
- 无登录态：服务端使用匿名 `session_id` cookie 隔离不同访客创建的 conversation。
- HTTP：`main.py` 中间件会在响应中补写缺失的 `session_id` cookie；但 `routers/conversations.py` 在读取时若仍无 cookie 会返回 **401**（例如被客户端禁用 cookie 等极端情况）。
- WebSocket 不经过上述 HTTP 中间件：连接必须在握手请求中携带有效 `session_id` cookie，否则服务端会推送失败状态后关闭连接（见下文）。

---

## `POST /conversations`

用途：创建一个新的对话窗口（conversation）。

请求体：无（或空 body）

成功响应（200，`CreateConversationResponse`）：
- `status`：`"created"`
- `conversationId`：string
- `title`：string 或 `null`
- `expoRoot`：string（`generated/<conversation-id>/baseExpo`）

错误响应（401）：
- 缺少 session cookie：`{ "detail": "Missing session cookie" }`（FastAPI 默认 JSON 结构）

---

## `GET /conversations`

用途：列出当前 `session_id` 下的所有 conversation（用于刷新后的窗口列表）。

成功响应（200，`ListConversationsResponse`）：
- `conversations`：数组（每项包含 `conversationId`、`title`、`createdAt`、`updatedAt`）

错误响应（401）：同上。

---

## `GET /conversations/{conversationId}`

用途：获取 conversation 基本信息。

成功响应（200，`CreateConversationResponse`）：
- `status`：`"created"`
- `conversationId`：string
- `title`：string 或 `null`
- `expoRoot`：string

错误响应：
- **401**：缺少 session cookie
- **404**：`{ "detail": "Conversation not found" }`（不属于当前 session 或不存在）

---

## `GET /conversations/{conversationId}/messages`

用途：获取该窗口的聊天气泡历史。

成功响应（200，`ConversationMessagesResponse`）：
- `conversationId`：string
- `title`：string 或 `null`
- `messages`：数组，每项：
  - `role`：`"system" | "user" | "assistant" | "tool"`
  - `content`：string
  - `toolCallId`：string 或 `null`（`tool` 角色时通常有值）

错误响应：**401**、**404**（conversation 不存在或不属于当前 session）

---

## `POST /conversations/{conversationId}/messages`

用途：追加一条用户消息，并创建一次 run（方案 A：一次用户消息 = 一次 run）。

请求体（JSON，`SendMessageRequest`）：
- `text`：string，必填（最小长度 1）
- `framework`：string，选填（默认 `expo`）
- `optionalTitle`：string，选填（预留字段，当前实现未强制使用）

成功响应（200，`SendMessageResponse`）：
- `status`：`"accepted"`
- `runId`：string

错误响应：
- **401**：缺少 session cookie
- **404**：conversation 不存在
- **409**：该 conversation 已有一个进行中的 run：`{ "detail": "A run is already running for this conversation" }`

说明：首轮发送时若 title 为空，服务端会用本条 `text` 截断生成 conversation 标题（最长约 60 字符加省略）。

---

## `WebSocket /conversations/ws/{conversationId}/{runId}`

用途：实时推送该 run 的 Agent 过程事件；run 完成后推送最终状态并关闭连接。

查询参数（Query）：
- `lastStepId`：number，可选，默认按 `0` 处理。断线重连时用于增量：仅推送 `stepId > lastStepId` 的事件（与同 `stepId` 内细粒度参数配合使用）。
- `lastEventSeq`：number，可选。同一 `stepId` 下多条事件时的精确增量游标；**未传时服务端为兼容旧客户端会采用保守策略（等价于从该 step 起补发该步内事件）**，避免漏掉 `command_output` 等同 step 多事件。传参时请与事件体中的 `eventSeq` 对齐。

连接校验失败时（在推送过程事件前）：
- 无 session cookie：推送 `{ "type": "task_status", "status": "failed", "error": "Missing session cookie" }` 后关闭
- conversation 不存在或不属于当前 session：推送 `{ "type": "task_status", "status": "failed", "error": "Conversation not found" }` 后关闭

消息推送：
- 过程事件（结构与任务接口类似，额外带序列号）：
  - `stepId`：int
  - `type`：`"round_start" | "llm_response" | "tool_call" | "tool_result" | "finished" | "command_start" | "command_output" | "command_end" | "expo_url_ready"`
  - `title`：string
  - `detail`：string 或 `null`
  - `eventSeq`：number 或 `null`（数据库中的同 step 内序号；旧数据可能为 `null`）

- 结束消息（当 run 已完成且本轮没有新事件时）：
  - `type`：`"task_status"`
  - `status`：`"completed"` 或 `"failed"`
  - `error`：string 或 `null`
  - `expoUrl`：string 或 `null`（当 `status="completed"` 时提供给前端「查看应用」的链接）

- 若 run 与 URL 中的 `conversationId` 不一致：推送 `task_status`/`failed`/`Run does not belong to conversation` 后关闭。

断开：
- 客户端主动断开时，服务端结束该连接的处理循环。

---

## 全局错误处理

未捕获的服务端异常会由 `main.py` 的异常处理器统一返回 **500**：
- `{ "error": "Internal Server Error" }`

（具体异常会打印到服务端控制台，响应中不暴露内部细节。）
