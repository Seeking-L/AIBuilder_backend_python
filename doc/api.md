#
# tasks 路由接口文档
# 本文件内容来自 `routers/tasks.py`。
#

## `POST /tasks/generate-app`

用途：同步方式生成应用（会阻塞直到任务完成），返回完整结果与过程事件列表。

请求体（JSON，`GenerateAppRequest`）：
- `description`：string，必填，最小长度 1
- `framework`：string，选填；默认通常为 `expo`

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
- `framework`：string，选填；默认通常为 `expo`

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
- 所有 `/conversations/*` 请求与 WebSocket 连接都会校验 `session_id`，未携带 cookie 的请求会失败。

---

## `POST /conversations`

用途：创建一个新的对话窗口（conversation）。

请求体：无（或空 body）

成功响应（200，`CreateConversationResponse`）：
- `status`：`"created"`
- `conversationId`：string
- `title`：string 或 `null`
- `expoRoot`：string（`generated/<conversation-id>/baseExpo`）

---

## `GET /conversations`

用途：列出当前 `session_id` 下的所有 conversation（用于刷新后的窗口列表）。

成功响应（200，`ListConversationsResponse`）：
- `conversations`：数组（每项包含 `conversationId/title/createdAt/updatedAt`）

---

## `GET /conversations/{conversationId}`

用途：获取 conversation 基本信息。

成功响应（200，`CreateConversationResponse`）：
- `status`：`"created"`
- `conversationId`：string
- `title`：string 或 `null`
- `expoRoot`：string

---

## `GET /conversations/{conversationId}/messages`

用途：获取该窗口的聊天气泡历史。

成功响应（200，`ConversationMessagesResponse`）：
- `conversationId`：string
- `title`：string 或 `null`
- `messages`：数组，每项：
  - `role`：`"user" | "assistant" | "tool"`
  - `content`：string
  - `toolCallId`：string（仅当 role 为 `tool` 时通常有值）

---

## `POST /conversations/{conversationId}/messages`

用途：追加一条用户消息，并创建一次 run（方案 A：一次用户消息 = 一次 run）。

请求体（JSON，`SendMessageRequest`）：
- `text`：string，必填（最小长度 1）
- `framework`：string，选填（默认 `expo`）
- `optionalTitle`：string，选填（当前实现未强制使用）

成功响应（200，`SendMessageResponse`）：
- `status`：`"accepted"`
- `runId`：string

---

## `WebSocket /conversations/ws/{conversationId}/{runId}`

用途：实时推送该 run 的 Agent 过程事件；run 完成后推送最终状态并关闭连接。

URL 参数：
- `lastStepId`：number，可选。用于断线重连的增量补发（仅推送 `stepId > lastStepId` 的新事件）。

消息推送：
- 过程事件（结构与任务接口保持一致，来自 `AgentEvent`）：
  - `stepId`：int
  - `type`：`"round_start" | "llm_response" | "tool_call" | "tool_result" | "finished" | "command_start" | "command_output" | "command_end" | "expo_url_ready"`
  - `title`：string
  - `detail`：string 或 `null`

- 结束消息（当 run 已完成且没有新事件时）：
  - `type`：`"task_status"`
  - `status`：`"completed"` 或 `"failed"`
  - `error`：string 或 `null`
  - `expoUrl`：string 或 `null`（当 `status="completed"` 时提供给前端“查看应用”按钮的跳转链接）

断开：
- 客户端主动断开时，服务端结束该连接的处理循环。

