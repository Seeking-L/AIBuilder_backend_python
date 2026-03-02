## AIBuilder_backend_python

基于 **FastAPI + Python** 的 Agent 后端，实现与原 `AIBuilder_backend`（Node + Express）基本兼容的接口与任务循环能力。

### 功能概览

- **健康检查**
  - `GET /health`
  - 返回：
    - `status: "ok"`
    - `workspaceRoot`: 当前工作区根路径（来自环境变量 `WORKSPACE_ROOT`，或默认 `../AIBuilder_workspace`）
- **任务生成接口**
  - `POST /tasks/generate-app`
  - 请求体（JSON）：
    - `description: string`（必填）
    - `framework?: string`（可选，默认 `expo`）
  - 返回体（JSON）：
    - `status: "completed"`
    - `description: string`
    - `framework: string`
    - `logs: string[]`（多轮对话与工具执行日志）
    - `summary: string`（最终总结文本）

### 目录结构

- `main.py`：FastAPI 应用入口（包含 `/health`，挂载 `/tasks` 路由）。
- `config.py`：加载 `.env`，提供全局配置（端口、工作区路径、LLM 配置等）。
- `models.py`：Pydantic 请求/响应模型（`GenerateAppRequest`, `GenerateAppResponse`）。
- `routers/`
  - `tasks.py`：实现 `POST /tasks/generate-app` 路由。
- `agent/`
  - `system_prompt.py`：生成系统提示词（包含 workspace root 与约束规则）。
  - `tools.py`：工具定义与实现（`execute_command`, `write_to_file`）。
  - `task_loop.py`：多轮「LLM 回复 + 工具执行」任务循环。
- `requirements.txt`：Python 依赖列表。

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

