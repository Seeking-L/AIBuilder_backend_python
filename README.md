# AIBuilder_backend_python

基于 **FastAPI + Python** 的后端：用「大模型 + 工具调用」在本地工作区里生成/修改 **Expo** 工程，并与原 **Node + Express** 版 `AIBuilder_backend` 在接口形态上基本对齐（便于同一套前端联调）。

---

## 一、如何把项目跑起来

### 1.1 环境要求

- Python 3.10+（建议与团队约定版本一致）
- 已安装对应大模型厂商的 **API Key**（见下文）

### 1.2 配置 `.env`

在项目根目录 **或上一级目录** 放置 `.env`（`config.py` 会依次尝试加载，便于与 Node 版共用一份配置）。常用变量如下：

| 变量 | 说明 |
|------|------|
| `PORT` | HTTP 端口，默认 `4000` |
| `WORKSPACE_ROOT` | 工作区根目录；未设置时默认为「本仓库上一级」下的 `AIBuilder_workspace` |
| `MODEL_PROVIDER` | `openai` / `kimi` / `qwen`（默认 `openai`） |
| `MODEL_NAME` | 模型名；未设置时默认 `gpt-4.1` |
| `OPENAI_API_KEY` | `MODEL_PROVIDER=openai` 时必填 |
| `KIMI_API_KEY` 或 `MOONSHOT_API_KEY` | `MODEL_PROVIDER=kimi` 时必填 |
| `DASHSCOPE_API_KEY` | `MODEL_PROVIDER=qwen` 时必填 |
| `CORS_ALLOW_ORIGINS` | 逗号分隔的前端源；需带 cookie 联调时不要依赖通配 `*` |
| `EXPO_LAN_HOST` | 手机 Expo Go 访问时，把 `exp://localhost:...` 改写成的局域网 IP/域名（多网卡/Docker 建议显式配置） |
| `MAX_TASK_ROUNDS` | Agent 单轮任务/单次 run 内 LLM↔工具 最大轮数，默认 `5` |
| `CONVERSATION_TTL_DAYS` / `CLEANUP_ON_STARTUP` | 对话过期清理：天数与是否在启动时执行清理 |

示例（可按实际修改）：

```bash
PORT=4000
WORKSPACE_ROOT=D:/MyCode/TryExpo/AIBuilder_workspace

MODEL_PROVIDER=kimi
MODEL_NAME=kimi-k2-turbo-preview
KIMI_API_KEY=你的密钥
```

### 1.3 安装依赖

**方式 A：Python 内置 venv（轻量，不依赖 Anaconda）**

```bash
cd AIBuilder_backend_python
python -m venv .venv
# Windows PowerShell：
.venv\Scripts\activate
pip install -r requirements.txt
```

**方式 B：Anaconda / Miniconda 独立环境**

适合已用 Conda 管理多项目 Python 的场景；环境与系统、`venv` 彼此隔离。

```bash
cd AIBuilder_backend_python

# 创建环境（Python 版本与「1.1 环境要求」一致即可，示例为 3.11）
conda create -n aibuilder-py python=3.11 -y

# 激活环境：在「Anaconda Prompt」里一般可直接用；在普通 PowerShell/CMD 中若提示无法识别 conda，
# 需先执行一次 conda 初始化，例如：conda init powershell，然后重开终端。
conda activate aibuilder-py

# 在本环境中安装项目依赖（仍使用 pip + requirements.txt，与方式 A 一致）
pip install -r requirements.txt
```

说明：

- 环境名 `aibuilder-py` 仅作示例，可改成任意名称。
- **每次新开终端**跑本项目前执行 `conda activate <环境名>`，再执行下文「1.4 启动服务」中的命令。
- 若希望严格用 conda 装包，也可在激活环境后 `conda install` 对应包，但需自行对齐 `requirements.txt` 版本；推荐仍以 `pip install -r requirements.txt` 为准，减少与文档不一致。

### 1.4 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 4000 --reload
```

若 `.env` 里配置了 `PORT`，也可用：

```bash
python main.py
```

（`main.py` 末尾会用 `settings.port` 启动 uvicorn。）

### 1.5 快速自检

```bash
curl http://localhost:4000/health
```

应返回 `status: ok` 以及当前解析到的 `workspaceRoot`。

### 1.6 Linux 上运行

与 **1.2～1.5** 的流程一致；以下为在 **Linux**（及多数 macOS 终端）上常见的写法与注意点。

#### 虚拟环境与 Python 命令

- 若系统只有 `python3` 而没有 `python`，请使用 `python3 -m venv .venv`，以及需要时改用 `python3 main.py`。
- 激活 venv 使用：**`source .venv/bin/activate`**（不要使用 Windows 下的 `.venv\Scripts\activate`）。
- 激活成功后，提示符前通常会出现 `(.venv)`，再执行 `pip install -r requirements.txt` 与下文启动命令即可。

#### `.env` 中的路径

- `WORKSPACE_ROOT` 请写成 **Unix 风格绝对路径**，例如：

```bash
WORKSPACE_ROOT=/home/yourname/TryExpo/AIBuilder_workspace
```

- 确保该目录存在，且运行后端的用户对其有 **读写权限**。若未设置 `WORKSPACE_ROOT`，默认会使用「本仓库上一级」下的 `AIBuilder_workspace`，同样需要可写。

#### 启动与自检

与 **1.4、1.5** 相同：在仓库根目录、已激活虚拟环境的前提下执行：

```bash
uvicorn main:app --host 0.0.0.0 --port 4000 --reload
# 若 .env 中已配置 PORT，也可：python3 main.py
curl http://localhost:4000/health
```

#### 其他说明

- **Conda**：在 Linux 上通常需先对当前 shell 执行一次 `conda init bash`（或 `zsh`）后重开终端，再 `conda activate <环境名>`；依赖安装仍建议以 `pip install -r requirements.txt` 为准，与 **1.3 方式 B** 一致。
- **Agent 内执行 Expo / Metro**：若工具会在工作区里调用 `npx expo` 等命令，本机需安装 **Node.js** 及相应 CLI，否则 `execute_command` 可能因命令不存在而失败。
- **手机 Expo Go 预览**：多网卡、虚拟机或 Docker 部署时，建议在 `.env` 中显式配置 `EXPO_LAN_HOST` 为本机局域网 IP 或可解析域名。

---

## 二、项目做什么、已实现能力、结构与运行流程

### 2.1 项目定位与已实现的主要功能

本仓库解决的是：**在后端可控的工作区内，让 LLM 通过结构化工具写文件、跑命令（含 Expo/Metro）**，并把过程以事件流形式交给前端展示；同时支持两种使用方式：

1. **任务模式（`/tasks`）**  
   - **同步**：`POST /tasks/generate-app` —— 一次请求内跑完整个 Agent 循环，HTTP 响应里直接带 `logs`、`summary`、`events`、`expoUrl` 等。  
   - **异步 + WebSocket**：`POST /tasks/generate-app-async` 先返回 `taskId`，客户端再连 `WebSocket /tasks/ws/{task_id}` 拉取增量 `AgentEvent`，结束时收到 `task_status` 后连接关闭。  
   - 每次任务会在 `WORKSPACE_ROOT/generated/<task-id>/` 下**新建**目录，并从 `BaseCodeForAI/baseExpo` **拷贝** Expo 模板到 `baseExpo`。

2. **对话窗口模式（`/conversations`）**  
   - 面向「多窗口、多轮聊天、刷新不丢历史」：用 **匿名 `session_id` Cookie** 隔离不同浏览器会话下的窗口列表。  
   - `POST /conversations` 创建窗口并初始化**持久化**工程目录；**只在首次**从模板拷贝 `baseExpo`，后续同一 `conversationId` 的多轮 run **复用同一工程**，不会反复清空。  
   - `POST /conversations/{conversationId}/messages` 发送用户消息 → 返回 `runId` → `WebSocket /conversations/ws/{conversationId}/{runId}` 订阅事件；支持查询参数 `lastStepId` / `lastEventSeq` **断线重连增量补发**。  
   - 消息、run 状态、Agent 事件写入 **SQLite**（默认 `WORKSPACE_ROOT/aibuilder.sqlite3`）；超长历史会触发 **摘要记忆**（`agent/summary_memory.py`），向模型注入压缩后的 system 摘要 + 最近若干条消息，避免上下文爆炸。  
   - 同一 `conversationId` 上若已有进行中的 run，新消息会 **409**，避免并发写同一 workspace。

3. **运维与可观测性**  
   - `agent.progress` 日志：默认写入仓库上一级 `logs/agent-progress.log`（可用 `PROGRESS_LOG_PATH` 覆盖），记录轮次、模型片段、工具调用、命令行输出等。  
   - `POST /maintenance/cleanup` 与可选的启动清理：按 `CONVERSATION_TTL_DAYS` 删除过期对话的 DB 记录及对应 `generated/<conversation-id>/` 目录。

4. **Agent 工具（`agent/tools.py`）**  
   当前暴露给模型的工具包括：`write_to_file`、`execute_command`（支持超时、`longRunning` 后台 dev server）、`get_available_port`、`notify_expo_url_ready`（把可用 `exp://` 以 `expo_url_ready` 事件形式贯通到前端）。  
   工作目录通过 **ContextVar** 按任务/对话 run 绑定到 `generated/...` 根路径，限制 LLM 只能在该沙箱内写文件、执行命令。

5. **Expo URL 与手机预览**  
   `agent/task_loop.py` 会把模型或 Metro 输出的 `exp://localhost/127.0.0.1` 等回环地址，在可能的情况下改写为 `EXPO_LAN_HOST` 或自动探测的局域网 IPv4，便于 Expo Go 扫码访问。

### 2.2 仓库目录结构（读代码时的地图）

```text
AIBuilder_backend_python/
├── main.py                 # FastAPI 应用工厂：CORS、匿名 session cookie、健康检查、挂载路由、DB 初始化、启动清理
├── config.py               # 从环境变量加载 Settings（端口、工作区、模型、SQLite、摘要阈值、CORS 等）
├── models.py               # Pydantic 请求/响应模型（与前端契约）
├── requirements.txt
├── routers/
│   ├── tasks.py            # /tasks/generate-app、generate-app-async、/tasks/ws/{task_id}
│   └── conversations.py    # /conversations*、消息发送、/conversations/ws/...
├── agent/
│   ├── task_loop.py        # 核心：ApiHandler（多厂商 OpenAI 兼容客户端）、run_task_loop、run_conversation_turn
│   ├── tools.py            # 工具定义与实现、workspace ContextVar
│   ├── system_prompt.py    # 系统提示词（与 Expo 目录约束、路由约定等）
│   ├── task_manager.py     # 内存态任务状态 + 事件追加（异步任务模式用）
│   ├── events.py           # AgentEvent 数据结构
│   └── summary_memory.py   # 对话过长时的摘要生成与拼接
├── storage/
│   └── sqlite_store.py     # conversations / messages / runs / agent_events 等表与访问接口
├── workspace/
│   └── conversation_workspace.py  # 按 conversationId 准备 generated/<id>/baseExpo（仅首次拷贝模板）
├── auth/
│   └── session.py          # session_id cookie 的生成与 WebSocket Cookie 解析
├── maintenance/
│   └── cleanup.py          # 过期对话：删库记录 + rmtree 工程目录
└── BaseCodeForAI/
    └── baseExpo/           # Expo 模板源（拷贝到各任务/各对话的工程目录）
```

### 2.3 HTTP / WebSocket 接口一览（便于对照路由代码）

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/health` | 探活 + 当前 `workspaceRoot` |
| POST | `/maintenance/cleanup` | 手动触发过期对话清理 |
| POST | `/tasks/generate-app` | 同步生成应用（阻塞至 Agent 结束） |
| POST | `/tasks/generate-app-async` | 异步启动任务，返回 `taskId` |
| WS | `/tasks/ws/{task_id}` | 异步任务的事件流 |
| POST | `/conversations` | 创建对话窗口 + 初始化 workspace |
| GET | `/conversations` | 列出当前 session 下窗口 |
| GET | `/conversations/{id}` | 窗口详情（含 `expoRoot`） |
| GET | `/conversations/{id}/messages` | 消息历史（UI 气泡） |
| POST | `/conversations/{id}/messages` | 发送用户消息，返回 `runId` |
| WS | `/conversations/ws/{conversation_id}/{run_id}` | 该 run 的 Agent 事件增量 + 结束 `task_status` |

更细的字段说明可参考 `doc/api.md`（若仓库内已维护）。

### 2.4 运行流程（建议按此顺序读源码）

#### 2.4.1 应用启动

`main.create_app()` 会：配置 `agent.progress` 文件日志；注册 CORS；注册 **HTTP 中间件**：若请求无 `session_id` cookie 则写入新 cookie（对话列表隔离依赖于此）；初始化 SQLite；可选执行 `cleanup_expired_conversations`；挂载 `routers/tasks.py` 与 `routers/conversations.py`。

#### 2.4.2 同步任务 `POST /tasks/generate-app`

```mermaid
sequenceDiagram
  participant Client
  participant Router as routers/tasks
  participant FS as 文件系统
  participant Loop as run_task_loop
  participant LLM as ApiHandler
  participant Tools as run_tool_call

  Client->>Router: JSON description/framework
  Router->>Router: uuid -> task_id
  Router->>FS: mkdir generated/task_id, copy BaseCodeForAI/baseExpo
  Router->>Loop: TaskInput(workspace, expo_root, task_id)
  loop 最多 MAX_TASK_ROUNDS 轮
    Loop->>LLM: chat.completions + tools
    LLM-->>Loop: assistant_text, tool_calls
    alt 无 tool_calls
      Loop-->>Router: final_text, events, logs
    else 有 tool_calls
      Loop->>Tools: 执行每个工具
      Tools-->>Loop: tool 结果写入 messages
    end
  end
  Router-->>Client: GenerateAppResponse
```

要点：`set_task_workspace_root` 把工具层 cwd 锁在 `generated/<task-id>`；`run_task_loop` 在 `agent/task_loop.py` 中完整展开；若带 `task_id`，事件还会进入 `task_manager` 供异步 WS 使用（同步接口本身不走 WS）。

#### 2.4.3 异步任务 `generate-app-async` + `WS /tasks/ws/{task_id}`

`BackgroundTasks` 中执行 `_run_task_in_background` → `run_task_loop`；循环内通过 `task_manager.append_event` 追加 `AgentEvent`。WebSocket 端轮询 `task_manager.get_state`，推送新事件，在 `completed/failed` 且无新事件时发 `task_status` 并关闭。

#### 2.4.4 对话：发消息 + 后台 run + WebSocket

```mermaid
sequenceDiagram
  participant Client
  participant Conv as routers/conversations
  participant Store as SqliteStore
  participant BG as BackgroundTasks
  participant Loop as run_conversation_turn

  Client->>Conv: POST .../messages
  Conv->>Conv: 同 conversation 锁：防并发 run
  Conv->>Store: append_message(user)
  Conv->>Store: create_run(running)
  Conv-->>Client: runId
  Conv->>BG: _background_run_conversation_turn

  BG->>Store: 可选：更新 conversation 摘要
  BG->>BG: 组装 existing_messages（摘要 system + 最近消息）
  BG->>Loop: event_sink 写 agent_events, persist_message 写 messages
  loop 与任务模式类似的 LLM 轮次
    Loop->>Loop: _emit_event -> 日志 + Store.append_agent_event
  end
  BG->>Store: set_run_status(completed/failed, expo_url, ...)
```

WebSocket 侧：`list_agent_events_incremental` 按 `lastStepId`/`lastEventSeq` 查库推送；run 结束后发 `task_status`（成功时带 `expoUrl`）。

#### 2.4.5 与「读代码」直接相关的几个文件

- **改 Agent 行为、轮数、厂商**：`agent/task_loop.py`（`ApiHandler`、`run_task_loop`、`run_conversation_turn`）、`config.py`。  
- **改工具能力与安全边界**：`agent/tools.py`。  
- **改提示词与目录约定**：`agent/system_prompt.py`、任务首轮 user 补丁 `_build_task_initial_user_content`（同在 `task_loop.py`）。  
- **改持久化字段或表结构**：`storage/sqlite_store.py`。  
- **改对话 API 语义**：`routers/conversations.py`、`models.py`。

### 2.5 多窗口与端到端验证（简版）

1. 两个浏览器上下文（或两次 `POST /conversations`）得到 `conversationIdA/B`，各自携带自己的 `session_id` cookie。  
2. 对 A 发消息 → 拿 `runId` → 连 WS 看 `AgentEvent`，结束看 `task_status`。  
3. 对 B 重复，确认工程目录与时间线互不干扰（A 的代码在 `generated/<conversationIdA>/baseExpo`）。  
4. 对 A 连续发多条消息，确认**不会**重复拷贝模板、目录一致。  
5. 刷新后 `GET /conversations` 与 `GET .../messages` 仍能恢复列表与历史。  
6. WS 断开重连时带 `?lastStepId=`（及可选 `lastEventSeq`）验证只补增量。

---

以上为当前实现的高度概括；具体字段与边界情况以 `models.py`、路由函数及 `SqliteStore` 为准。
