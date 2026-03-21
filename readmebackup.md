## AIBuilder_backend_python

基于 **FastAPI + Python** 的 Agent 后端，实现与原 `AIBuilder_backend`（Node + Express）基本兼容的接口与任务循环能力。

### 环境配置（.env）

可以直接复用原 Node 版项目根目录下的 `.env`，关键字段包括：

```bash
PORT=4000
WORKSPACE_ROOT=D:/MyCode/TryExpo/AIBuilder_workspace

# 模型与 Provider（与 Node 版保持一致）
MODEL_PROVIDER=kimi          # 或 openai
MODEL_NAME=kimi-k2-turbo-preview

# 对应 Provider 的 API KEY
KIMI_API_KEY=...
# 或者：
# MOONSHOT_API_KEY=...
# OPENAI_API_KEY=...
```

### 安装与运行

1. 创建虚拟环境并安装依赖：

```bash
cd AIBuilder_backend_python
python -m venv .venv
.venv\Scripts\activate  # Windows PowerShell
pip install -r requirements.txt
```

2. 确认 `.env` 已配置在当前目录或上级目录（可直接复用 Node 版 `.env`）。

3. 启动服务：

(conda activate aibuilder-py)
```bash
uvicorn main:app --host 0.0.0.0 --port 4000 --reload
```

> 如需修改端口，可在 `.env` 中设置 `PORT`，`main.py` 会自动读取。

### 调用示例

#### 健康检查

```bash
curl http://localhost:4000/health
```

#### 生成应用

```bash
curl -X POST http://localhost:4000/tasks/generate-app ^
  -H "Content-Type: application/json" ^
  -d "{\"description\": \"生成一个简单的 Expo 计数器页面\", \"framework\": \"expo\"}"
```

返回示例（结构上与 Node 版保持一致）：

```json
{
  "status": "completed",
  "description": "生成一个简单的 Expo 计数器页面",
  "framework": "expo",
  "logs": [
    "...",
    "..."
  ],
  "summary": "..."
}
```

### 多窗口对话验证（端到端）

说明：对话窗口/多轮聊天使用匿名 `session_id` cookie 隔离，不需要登录态。

1. 创建两个窗口（conversation A/B）
   - 调用 `POST /conversations` 两次，分别获得 `conversationIdA`、`conversationIdB`
   - 前端/浏览器会自动保存 cookie；如果用命令行，请确保携带同一个 cookie。

2. 在窗口 A 发送第一条消息并实时订阅
   - `POST /conversations/{conversationIdA}/messages`，请求体：`{"text":"你的第一个需求"}`（可选携带 framework）
   - 得到 `runIdA`
   - 连接 `WebSocket /conversations/ws/{conversationIdA}/{runIdA}`，观察：
     - 实时收到 `AgentEvent(stepId/type/title/detail)`（直到有 tool / 命令输出）
     - run 结束后收到一条 `{"type":"task_status","status":"completed"|"failed","error":...}`，随后连接关闭

3. 切换到窗口 B 重复上述流程
   - 确认 A 与 B 的时间线与工程修改互不干扰。

4. 同一窗口追加多条消息（验证工程复用）
   - 继续对 `conversationIdA` 调用 `POST /conversations/{conversationIdA}/messages`
   - 在观察到更多 `write_to_file/execute_command` 修改后，确认修改发生在同一个目录：
     - `generated/<conversationIdA>/baseExpo`（不应反复拷贝模板/清空目录）

5. 刷新页面（验证窗口列表与历史持久化）
   - 刷新后调用 `GET /conversations`：应能看到之前创建的 A/B
   - 调用 `GET /conversations/{conversationIdA}/messages`：应能看到完整历史（包含用户与 AI 回复、tool 结果）

6. 断线重连（验证 lastStepId 增量补发）
   - 在 WebSocket 连接过程中主动断开
   - 重新连接时使用 `?lastStepId=<最后收到的stepId>`：应只补齐缺失 events，且不重复已收到的 events。

