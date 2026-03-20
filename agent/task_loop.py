from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, TypedDict

from openai import OpenAI

from config import settings
from .events import AgentEvent
from .system_prompt import get_system_prompt
from .task_manager import task_manager
from .tools import (
    run_tool_call,
    tool_definitions,
    set_task_workspace_root,
    reset_task_workspace_root,
)


class ToolCall(TypedDict):
    id: str
    type: Literal["function"]

    class Function(TypedDict):
        name: str
        arguments: str

    function: Function


class ChatMessage(TypedDict, total=False):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    # assistant with tool calls
    tool_calls: List[ToolCall]
    # tool message
    tool_call_id: str


@dataclass
class TaskInput:
    description: str
    framework: Optional[str] = None
    # 单次任务的标识，用于日志与生成目录（如 generated/<task-id>）
    task_id: Optional[str] = None
    # 若设置，则表示本次任务的工作区根目录覆盖值（绝对路径字符串）
    workspace_root_override: Optional[str] = None
    # 本次任务对应的 Expo 应用根目录（绝对路径字符串），例如 generated/<task-id>/baseExpo
    expo_root: Optional[str] = None


@dataclass
class TaskResult:
    logs: List[str]
    final_text: str
    # 结构化的过程事件，方便前端以时间线方式展示 AI 的工作过程
    events: List[AgentEvent]
    # 回传任务上下文，便于上层路由使用
    task_id: Optional[str] = None
    expo_root: Optional[str] = None
    # 尝试从工具输出或模型回复中提取到的 Expo URL（如 exp://...）
    expo_url: Optional[str] = None


class ApiHandler:
    """Python 版 LLM 封装，参考 TypeScript 的 ApiHandler 实现。

    这里做的事情可以概括为三步：
    1. 根据环境变量中的 MODEL_PROVIDER / 各类 API key，初始化对应厂商的 OpenAI 兼容客户端；
    2. 提供 `_to_openai_messages` 工具方法，把我们内部定义的 `ChatMessage` 转成 OpenAI SDK 期望的格式；
    3. 在 `create_message` 中发起一次对话，并把返回结果里的文本和 tool_calls 抽取成易于后续循环处理的结构。
    """

    def __init__(self) -> None:
        provider = settings.model_provider
        if provider == "openai":
            if not settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is required when MODEL_PROVIDER=openai")
            self.client = OpenAI(api_key=settings.openai_api_key)
        elif provider == "kimi":
            if not settings.kimi_api_key:
                raise RuntimeError(
                    "KIMI_API_KEY or MOONSHOT_API_KEY is required when MODEL_PROVIDER=kimi"
                )
            # Kimi 使用 OpenAI 兼容协议，通过 base_url 指向 Moonshot 平台
            self.client = OpenAI(
                api_key=settings.kimi_api_key,
                base_url="https://api.moonshot.cn/v1",
            )
        elif provider == "qwen":
            # 千问（DashScope）模式必须提供 DASHSCOPE_API_KEY
            if not settings.qwen_api_key:
                raise RuntimeError("DASHSCOPE_API_KEY is required when MODEL_PROVIDER=qwen")
            # 千问提供 OpenAI 兼容接口，通过 base_url 指向 DashScope 平台
            self.client = OpenAI(
                api_key=settings.qwen_api_key,
                base_url=settings.dashscope_base_url,
            )
        else:
            raise RuntimeError(f"Unsupported model provider: {provider}")

    def _to_openai_messages(self, messages: List[ChatMessage]) -> List[Dict[str, Any]]:
        """将内部 ChatMessage 列表转换为 OpenAI SDK 所需的消息格式。

        - 统一处理 user / assistant / system / tool 四种角色；
        - 当 assistant 携带 tool_calls 时，需要把 tool_calls 按 OpenAI 协议展开；
        - tool 消息需要带上 `tool_call_id`，以便模型知道它对应哪次工具调用。
        """
        result: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg["role"]
            if role in ("user", "assistant", "system") and "tool_calls" not in msg:
                result.append({"role": role, "content": msg["content"]})
            elif role == "assistant" and "tool_calls" in msg:
                tool_calls_payload = []
                for tc in msg["tool_calls"]:
                    tool_calls_payload.append(
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                    )
                result.append(
                    {
                        "role": "assistant",
                        "content": msg["content"],
                        "tool_calls": tool_calls_payload,
                    }
                )
            elif role == "tool":
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg["tool_call_id"],
                        "content": msg["content"],
                    }
                )
        return result

    def create_message(
        self,
        system_prompt: str,
        messages: List[ChatMessage],
        tools: List[Dict[str, Any]],
    ) -> tuple[str, List[ToolCall]]:
        # 在每一轮对话最前面插入 system prompt，告诉 LLM 当前的工作模式和能力边界
        full_messages = [{"role": "system", "content": system_prompt}]
        full_messages.extend(self._to_openai_messages(messages))

        # 调用 OpenAI 兼容接口，开启一轮对话
        response = self.client.chat.completions.create(
            model=settings.model_name,
            messages=full_messages,  # type: ignore[arg-type]
            tools=tools,
        )

        # 简化处理：目前只取第一条 choice 作为模型输出
        choice = response.choices[0]
        message = choice.message

        # 安全地解析返回的 assistant 文本：
        # - 常见情况：content 是字符串，直接使用；
        # - 少数情况下是结构化内容，这里统一 JSON 序列化成字符串。
        text = ""
        if message.content:
            # 在多数 provider 中 content 为字符串；如为其他结构则序列化
            if isinstance(message.content, str):
                text = message.content
            else:
                text = json.dumps(message.content, ensure_ascii=False)

        # 将 SDK 返回的 tool_calls 转成我们内部的 `ToolCall` 结构，
        # 方便 run_task_loop 中统一遍历和执行。
        tool_calls: List[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                if tc.type != "function":
                    continue
                fn = tc.function
                arguments = fn.arguments
                if isinstance(arguments, str):
                    args_str = arguments
                else:
                    args_str = json.dumps(arguments, ensure_ascii=False)
                tool_calls.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": fn.name,
                            "arguments": args_str,
                        },
                    }
                )

        return text, tool_calls


def _extract_expo_url(text: str) -> Optional[str]:
    """从文本中尝试提取 Expo URL（例如 exp:// 开头的链接）。"""
    # 优先匹配 exp:// 链接
    match = re.search(r"(exp://[^\s\"']+)", text)
    if match:
        return match.group(1)
    # 兜底：有些情况下可能返回本地 Web URL
    match = re.search(r"(http://localhost:\d+[^\s\"']*)", text)
    if match:
        return match.group(1)
    return None


def _build_task_initial_user_content(
    *,
    description: str,
    framework: Optional[str],
    task_id: Optional[str],
    workspace_root: Path,
    expo_root: Optional[str],
) -> str:
    """构造“一次性任务”run 的第一条 user 消息内容。

    说明：
    - run_task_loop 目前只支持一次性 description 驱动，因此首条 user 消息会附带较多“开发约束文本”。
    - conversation 模式会复用系统 prompt + messages 历史，不再依赖这段 user content 里的长约束文本（后续可按需要调整）。
    """

    effective_expo_root = expo_root or "(not provided)"
    return "\n".join(
        [
            f"User request: {description}",
            f"Preferred framework: {framework or 'expo'}",
            f"Task id: {task_id or 'N/A'}",
            f"Workspace root for this task: {workspace_root}",
            f"Expo app root for this task: {effective_expo_root}",
            "",
            "Development constraints for this task:",
            "- You must treat the Expo app root for this task as a project copied from the shared template at BaseCodeForAI/baseExpo into generated/<task-id>/baseExpo under the workspace root.",
            "- All modifications for this task must stay under this per-task Expo app root; do not modify any shared baseExpo directories such as AIBuilder_workspace/baseExpo or BaseCodeForAI/baseExpo.",
            "- Only create or modify files under the Expo app root in these subdirectories: app/, components/common/, hooks/, services/, types/.",
            "- Do NOT modify configuration or tooling files such as package.json, tsconfig.json, ESLint configs, or scripts (e.g. scripts/reset-project.js).",
            "- Use Expo Router file-based routing for pages (e.g. app/profile/index.tsx, app/posts/[id].tsx).",
            "- Use ScreenContainer, AppText, PrimaryButton, and Spacer from components/common for layout and UI where appropriate.",
            "- Put network and data access logic into modules under services/; screens should not call fetch directly.",
            "- Before running the Expo dev server, run `npm ci` in the Expo app root to install dependencies.",
            "- Start the Expo dev server with `npm start` or `npx expo start --tunnel` using cwd set to the Expo app root.",
            "",
            "When planning your work, first outline which files you intend to add or modify (with paths relative to the Expo app root, e.g. app/... under baseExpo for this task),",
            "then use the available tools to actually write files and run commands.",
        ]
    )


def run_task_loop(input: TaskInput) -> TaskResult:
    """多轮「LLM 回复 + 工具执行」任务循环，参考 TS 版本的 runTaskLoop。

    整体执行流程：
    1. 把前端传入的需求文案（description / framework）组装成首轮 user 消息；
    2. 最多进行若干轮循环（由 settings.max_task_rounds 控制）：
       - 调用 LLM，拿到 assistant 文本 + 可能的 tool_calls；
       - 若没有 tool_calls，说明模型已经给出最终答案，循环提前结束；
       - 若有 tool_calls，则依次执行每个工具，把结果再作为 tool 消息反馈给 LLM；
    3. 把每一轮的日志（assistant 回复、工具执行情况）记录在 logs 中，最终返回给前端。
    """
    api = ApiHandler()

    # 计算本次任务的实际工作区根目录：
    # - 若调用方提供了 workspace_root_override，则优先使用（通常为 AIBuilder_workspace/generated/<task-id>）；
    # - 否则退回到全局 settings.workspace_root。
    if input.workspace_root_override:
        effective_workspace_root = (
            Path(input.workspace_root_override).expanduser().resolve()
        )
    else:
        effective_workspace_root = settings.workspace_root.resolve()

    # 将本次任务的 workspace_root 注入到工具层的上下文中，使 execute_command / write_to_file
    # 只能在该任务的 generated/<task-id> 工作区内执行。
    workspace_token: object | None = None
    try:
        workspace_token = set_task_workspace_root(effective_workspace_root)

        system_prompt = get_system_prompt(
            workspace_root=effective_workspace_root,
            expo_root=Path(input.expo_root).resolve() if input.expo_root else None,
            task_id=input.task_id,
        )

        # 收集调试 / 展示用日志；会在 HTTP 接口返回给前端，方便用户查看每一轮行为。
        logs: List[str] = []
        # 结构化的事件列表，按 step_id 递增，方便前端渲染时间线。
        events: List[AgentEvent] = []
        step_id = 1
        # 对话上下文，按照 OpenAI chat 格式组织。
        # 这里只在一开始塞入一条 user 消息，后续每一轮会 append assistant / tool 消息。
        messages: List[ChatMessage] = [
            {
                "role": "user",
                "content": _build_task_initial_user_content(
                    description=input.description,
                    framework=input.framework,
                    task_id=input.task_id,
                    workspace_root=effective_workspace_root,
                    expo_root=input.expo_root,
                ),
            }
        ]

        # 最终要返回给前端的自然语言总结（由最后一轮模型回复填充）
        final_text = ""

        # 限制最大轮数，避免模型或工具异常导致无限循环。
        for round_index in range(settings.max_task_rounds):
            round_number = round_index + 1
            logs.append(f"--- Round {round_number} ---")
            round_event = AgentEvent(
                step_id=step_id,
                type="round_start",
                title=f"开始第 {round_number} 轮对话",
                detail=None,
            )
            events.append(round_event)
            if input.task_id:
                task_manager.append_event(input.task_id, round_event)
            step_id += 1

            # 发起一轮对话，传入当前累积的 messages 和可用工具定义
            assistant_text, tool_calls = api.create_message(
                system_prompt=system_prompt,
                messages=messages,
                tools=tool_definitions,
            )

            # 记录本轮 assistant 输出，便于在前端完整展示
            logs.append(f"Assistant:\n{assistant_text}")
            llm_event = AgentEvent(
                step_id=step_id,
                type="llm_response",
                title=f"模型回复（第 {round_number} 轮）",
                detail=assistant_text[:400],
            )
            events.append(llm_event)
            if input.task_id:
                task_manager.append_event(input.task_id, llm_event)
            step_id += 1

            # 当模型没有再请求调用工具时，认为已经得到最终结果，可以提前结束循环
            if not tool_calls:
                final_text = assistant_text
                finished_event = AgentEvent(
                    step_id=step_id,
                    type="finished",
                    title="任务已完成（未再请求工具）",
                    detail=final_text[:400],
                )
                events.append(finished_event)
                if input.task_id:
                    task_manager.append_event(input.task_id, finished_event)
                step_id += 1
                break

            # 保存每个工具调用的输出 / 错误文本，用于后续作为 tool 消息反馈给 LLM
            tool_results: List[str] = []

            # 依次执行每个工具调用，并把执行日志写入 logs
            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                logs.append(f"Tool call: {tool_name}")
                call_event = AgentEvent(
                    step_id=step_id,
                    type="tool_call",
                    title=f"调用工具：{tool_name}",
                    detail=None,
                )
                events.append(call_event)
                if input.task_id:
                    task_manager.append_event(input.task_id, call_event)
                step_id += 1

                # 为 execute_command 准备流式回调（实时推送命令与输出）
                command_step_id = step_id - 1
                execute_command_hooks = None
                if (
                    tool_name == "execute_command"
                    and input.task_id
                ):
                    try:
                        arguments = json.loads(
                            tool_call["function"].get("arguments") or "{}"
                        )
                        command_str = str(arguments.get("command") or "").strip()
                        # 尝试从命令中解析端口号，用于记录到 dev server registry。
                        # 这里采用最常见的 `--port <number>` 形式进行正则提取。
                        dev_server_port: Optional[int] = None
                        if "expo start" in command_str and "--port" in command_str:
                            m = re.search(r"--port\s+(\d+)", command_str)
                            if m:
                                try:
                                    dev_server_port = int(m.group(1))
                                except ValueError:
                                    dev_server_port = None
                    except (json.JSONDecodeError, TypeError):
                        command_str = "(无法解析命令)"
                        dev_server_port = None

                    def _on_start(cmd: str) -> None:
                        ev = AgentEvent(
                            step_id=command_step_id,
                            type="command_start",
                            title="执行命令",
                            detail=cmd or None,
                        )
                        task_manager.append_event(input.task_id, ev)
                        # 如果当前命令看起来是在启动 Expo dev server，则在任务状态中记录一次启动尝试。
                        if "expo start" in cmd:
                            task_manager.register_dev_server_start(
                                input.task_id,
                                port=dev_server_port,
                                command=cmd,
                                pid=None,
                            )

                    def _on_output(chunk: str, stream: str) -> None:
                        ev = AgentEvent(
                            step_id=command_step_id,
                            type="command_output",
                            title=stream,
                            detail=chunk or None,
                        )
                        task_manager.append_event(input.task_id, ev)
                        # 当输出中出现典型的“Waiting on http://localhost:<port>”提示时，
                        # 说明 dev server 已经成功进入监听状态，可以将其标记为 running。
                        text = chunk or ""
                        if "Waiting on http://localhost:" in text:
                            task_manager.register_dev_server_running(input.task_id)

                    def _on_end(exit_code: int, _out: str, _err: str) -> None:
                        # 对于 execute_command，特别是长时间运行的 dev server，补充更清晰的退出含义说明。
                        if exit_code == -1:
                            detail = "Exit code: -1 (the process was likely killed by a timeout or external signal)."
                        else:
                            detail = f"Exit code: {exit_code}"
                        ev = AgentEvent(
                            step_id=command_step_id,
                            type="command_end",
                            title="命令结束",
                            detail=detail,
                        )
                        task_manager.append_event(input.task_id, ev)
                        # 无论退出原因如何，命令结束时 dev server 都不再处于运行状态。
                        if "expo start" in (command_str or ""):
                            task_manager.register_dev_server_stopped(
                                input.task_id,
                                failed=(exit_code != 0),
                            )

                    def _on_command_input_request(prompt_text: str) -> str:
                        """当子进程请求 stdin 输入时，调用 LLM 自动生成最小输入内容。

                        该回调会被 `agent/tools.py` 在“子进程输出线程”中触发，因此：
                        - 不修改 messages/main loop；
                        - 禁用 tools（避免模型再触发 execute_command 等递归调用）；
                        - 要求模型只输出可直接写入 stdin 的内容（无解释）。
                        """
                        input_system_prompt = (
                            "You are an automated stdin responder for interactive CLI prompts.\n"
                            "You will receive a detected prompt text from a running process.\n\n"
                            "Rules (VERY IMPORTANT):\n"
                            "- Output ONLY the exact input the user should type, with no explanation.\n"
                            "- For yes/no prompts: output only a single character: 'y' or 'n'.\n"
                            "- For port-replacement prompts: default to 'y'.\n"
                            "- For Expo 'Press <key>' prompts: output exactly one key character from the candidate list.\n"
                            "- If multiple candidates exist and 'w' exists, prefer 'w' (open web). Otherwise choose the first candidate.\n"
                            "- Output must be a single line.\n"
                        )
                        try:
                            # 禁用工具调用，避免模型递归触发 execute_command。
                            _assistant_text, _tool_calls = api.create_message(
                                system_prompt=input_system_prompt,
                                messages=[{"role": "user", "content": prompt_text}],
                                tools=[],
                            )
                            # 工具层会根据 kind 做进一步 sanitize/兜底。
                            return (_assistant_text or "").strip()
                        except Exception:
                            # 回调失败时返回空字符串，让工具层兜底策略接管。
                            return ""

                    execute_command_hooks = (
                        _on_start,
                        _on_output,
                        _on_end,
                        _on_command_input_request,
                    )

                try:
                    result = run_tool_call(
                        tool_call,
                        execute_command_hooks=execute_command_hooks,
                    )
                    wrapped = (
                        f"Tool {tool_call['function']['name']} (id={tool_call['id']}) "
                        f"result:\n{result}"
                    )
                    tool_results.append(wrapped)
                    result_summary = str(result)
                except Exception as exc:  # noqa: BLE001
                    error_text = (
                        f"Tool {tool_call['function']['name']} (id={tool_call['id']}) error: {exc}"
                    )
                    tool_results.append(error_text)
                    logs.append(error_text)
                    result_summary = error_text

                result_event = AgentEvent(
                    step_id=step_id,
                    type="tool_result",
                    title=f"工具 {tool_name} 执行完成",
                    detail=result_summary[:400],
                )
                events.append(result_event)
                if input.task_id:
                    task_manager.append_event(input.task_id, result_event)
                step_id += 1

            # 将本轮 assistant 的回复（含 tool_calls）加入对话上下文，
            # 这样下一轮模型可以看到自己上一次下发的工具调用。
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_text,
                    "tool_calls": tool_calls,
                }
            )

            # 把每个工具的执行结果写回为一条 tool 消息，
            # `tool_call_id` 用于和对应的 assistant tool_call 进行匹配。
            for call, result in zip(tool_calls, tool_results, strict=False):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    }
                )

        # 若没有在循环中显式标记 finished，则这里兜底追加一个结束事件
        if not any(evt.type == "finished" for evt in events):
            finished_event = AgentEvent(
                step_id=step_id,
                type="finished",
                title="任务已结束（达到最大轮数或未生成最终总结）",
                detail=final_text[:400] if final_text else None,
            )
            events.append(finished_event)
            if input.task_id:
                task_manager.append_event(input.task_id, finished_event)

        # 在所有轮次结束后，尝试从日志与最终回复中提取 Expo URL
        joined_text = "\n".join(logs + [final_text])
        expo_url = _extract_expo_url(joined_text)

        return TaskResult(
            logs=logs,
            final_text=final_text,
            events=events,
            task_id=input.task_id,
            expo_root=input.expo_root,
            expo_url=expo_url,
        )
    finally:
        # 确保无论任务是否成功完成，都重置 workspace_root 上下文，避免影响其它任务。
        if workspace_token is not None:
            reset_task_workspace_root(workspace_token)


def run_conversation_turn(
    *,
    run_id: str,
    workspace_root_override: str,
    expo_root: str,
    existing_messages: List[ChatMessage],
    event_sink: Callable[[AgentEvent], None],
    persist_message: Callable[[str, str, Optional[str], Optional[List[ToolCall]]], None],
) -> TaskResult:
    """conversation 模式：一次用户消息对应一次 run。

    输入：
    - existing_messages：已包含“本次用户消息”在内的完整对话上下文（已按 OpenAI chat 格式保存）。
    - event_sink：把每个 AgentEvent 立即写入持久化存储（满足刷新后时间线）。
    - persist_message：把 run 过程中新增的 assistant/tool 消息写入持久化存储（供下次续聊构造上下文）。
    """

    api = ApiHandler()

    effective_workspace_root = Path(workspace_root_override).expanduser().resolve()
    workspace_token: object | None = None
    try:
        workspace_token = set_task_workspace_root(effective_workspace_root)

        system_prompt = get_system_prompt(
            workspace_root=effective_workspace_root,
            expo_root=Path(expo_root).resolve(),
            task_id=run_id,
        )

        logs: List[str] = []
        events: List[AgentEvent] = []
        step_id = 1

        # 用于“AI 生成结束后通知前端查看应用”的贯通逻辑：
        # - 当模型调用 `notify_expo_url_ready` tool 时，后端把 exp://... 转成前端可见事件
        # - 同一次 run 里只通知一次（避免重复按钮/多次跳转）
        expo_url_ready: Optional[str] = None
        expo_url_ready_notified = False

        # 注意：existing_messages 必须包含本次 user 输入；run 不会再创建新的 user 消息。
        messages: List[ChatMessage] = list(existing_messages)

        final_text = ""

        for round_index in range(settings.max_task_rounds):
            round_number = round_index + 1
            logs.append(f"--- Round {round_number} ---")
            round_event = AgentEvent(
                step_id=step_id,
                type="round_start",
                title=f"开始第 {round_number} 轮对话",
                detail=None,
            )
            events.append(round_event)
            event_sink(round_event)
            step_id += 1

            assistant_text, tool_calls = api.create_message(
                system_prompt=system_prompt,
                messages=messages,
                tools=tool_definitions,
            )

            logs.append(f"Assistant:\n{assistant_text}")
            llm_event = AgentEvent(
                step_id=step_id,
                type="llm_response",
                title=f"模型回复（第 {round_number} 轮）",
                detail=assistant_text[:400],
            )
            events.append(llm_event)
            event_sink(llm_event)
            step_id += 1

            if not tool_calls:
                final_text = assistant_text

                # 关键：conversation 模式需要持久化最终 assistant 消息（无 tool_calls）。
                # 否则刷新后聊天记录会缺少最后一条 AI 回复，且后续续聊缺少上下文。
                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_text,
                    }
                )
                persist_message("assistant", assistant_text, None, None)

                finished_event = AgentEvent(
                    step_id=step_id,
                    type="finished",
                    title="任务已完成（未再请求工具）",
                    detail=final_text[:400],
                )
                events.append(finished_event)
                event_sink(finished_event)
                step_id += 1
                break

            tool_results: List[str] = []
            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                # 只有 notify_expo_url_ready 这个 tool 才需要解析 expoUrl 参数；
                # 其它工具保持 None，避免无谓的解析和潜在异常。
                expo_url_candidate: Optional[str] = None
                if tool_name == "notify_expo_url_ready":
                    arguments_json = tool_call["function"].get("arguments") or "{}"
                    try:
                        arguments_obj = json.loads(arguments_json)
                    except (json.JSONDecodeError, TypeError):
                        arguments_obj = {}

                    candidate = arguments_obj.get("expoUrl") or arguments_obj.get(
                        "expo_url"
                    )
                    if isinstance(candidate, str) and candidate.strip():
                        expo_url_candidate = candidate.strip()

                    # 兜底：模型可能只在自然语言里给了 exp://...，而 tool 参数为空
                    if not expo_url_candidate:
                        expo_url_candidate = _extract_expo_url(assistant_text)
                    if not expo_url_candidate:
                        # 再兜底：从最近一小段日志里提取（避免 join(logs) 过大）
                        expo_url_candidate = _extract_expo_url(
                            "\n".join(logs[-10:])
                        )

                    # 基本校验：要求以 exp:// 开头（前端“查看应用”会直接交给 Expo Go）
                    if not (expo_url_candidate and expo_url_candidate.startswith("exp://")):
                        expo_url_candidate = None

                logs.append(f"Tool call: {tool_name}")
                call_event = AgentEvent(
                    step_id=step_id,
                    type="tool_call",
                    title=f"调用工具：{tool_name}",
                    detail=None,
                )
                events.append(call_event)
                event_sink(call_event)
                step_id += 1

                command_step_id = step_id - 1
                execute_command_hooks = None

                if tool_name == "execute_command":
                    try:
                        arguments = json.loads(
                            tool_call["function"].get("arguments") or "{}"
                        )
                        command_str = str(arguments.get("command") or "").strip()
                    except (json.JSONDecodeError, TypeError):
                        command_str = "(无法解析命令)"

                    def _on_start(cmd: str) -> None:
                        ev = AgentEvent(
                            step_id=command_step_id,
                            type="command_start",
                            title="执行命令",
                            detail=cmd or None,
                        )
                        event_sink(ev)

                    def _on_output(chunk: str, stream: str) -> None:
                        ev = AgentEvent(
                            step_id=command_step_id,
                            type="command_output",
                            title=stream,
                            detail=chunk or None,
                        )
                        event_sink(ev)

                    def _on_end(exit_code: int, _out: str, _err: str) -> None:
                        if exit_code == -1:
                            detail = "Exit code: -1 (killed by timeout or signal)."
                        else:
                            detail = f"Exit code: {exit_code}"
                        ev = AgentEvent(
                            step_id=command_step_id,
                            type="command_end",
                            title="命令结束",
                            detail=detail,
                        )
                        event_sink(ev)

                    def _on_command_input_request(prompt_text: str) -> str:
                        """conversation 模式下的 stdin 自动应答回调。"""
                        input_system_prompt = (
                            "You are an automated stdin responder for interactive CLI prompts.\n"
                            "Output ONLY the exact input to write to stdin (no explanation).\n\n"
                            "- yes/no prompts: output only 'y' or 'n'.\n"
                            "- port replacement prompts: default 'y'.\n"
                            "- Expo 'Press <key>' prompts: output exactly one key from candidate list; prefer 'w' if present.\n"
                        )
                        try:
                            _assistant_text, _tool_calls = api.create_message(
                                system_prompt=input_system_prompt,
                                messages=[{"role": "user", "content": prompt_text}],
                                tools=[],
                            )
                            return (_assistant_text or "").strip()
                        except Exception:
                            return ""

                    execute_command_hooks = (
                        _on_start,
                        _on_output,
                        _on_end,
                        _on_command_input_request,
                    )

                try:
                    result = run_tool_call(
                        tool_call,
                        execute_command_hooks=execute_command_hooks,
                    )
                    wrapped = (
                        f"Tool {tool_call['function']['name']} (id={tool_call['id']}) "
                        f"result:\n{result}"
                    )
                    tool_results.append(wrapped)
                    result_summary = str(result)
                except Exception as exc:  # noqa: BLE001
                    error_text = (
                        f"Tool {tool_call['function']['name']} (id={tool_call['id']}) error: {exc}"
                    )
                    tool_results.append(error_text)
                    logs.append(error_text)
                    result_summary = error_text

                result_event = AgentEvent(
                    step_id=step_id,
                    type="tool_result",
                    title=f"工具 {tool_name} 执行完成",
                    detail=result_summary[:400],
                )
                events.append(result_event)
                event_sink(result_event)

                # 当模型调用 notify_expo_url_ready 且 expo_url 尚未通知过时，
                # 追加一个可被前端渲染的“查看应用”事件。
                if (
                    tool_name == "notify_expo_url_ready"
                    and expo_url_candidate
                    and not expo_url_ready_notified
                ):
                    expo_url_ready = expo_url_candidate
                    expo_url_ready_notified = True

                    expo_event = AgentEvent(
                        step_id=step_id + 1,
                        type="expo_url_ready",
                        title="查看应用",
                        detail=expo_url_candidate,
                    )
                    events.append(expo_event)
                    event_sink(expo_event)
                    step_id += 2
                else:
                    step_id += 1

            # 把本轮 assistant 回复（含 tool_calls）加入对话上下文
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_text,
                    "tool_calls": tool_calls,
                }
            )
            persist_message("assistant", assistant_text, None, tool_calls)

            # 把每个工具的执行结果加入 tool 消息上下文
            for call, result in zip(tool_calls, tool_results, strict=False):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    }
                )
                persist_message("tool", result, call["id"], None)

        # 若没有显式标记 finished，则兜底追加结束事件
        if not any(evt.type == "finished" for evt in events):
            finished_event = AgentEvent(
                step_id=step_id,
                type="finished",
                title="任务已结束（达到最大轮数）",
                detail=final_text[:400] if final_text else None,
            )
            events.append(finished_event)
            event_sink(finished_event)

        joined_text = "\n".join(logs + [final_text])
        extracted_expo_url = _extract_expo_url(joined_text)
        # 优先使用 tool 通知得到的 expo_url，确保与前端按钮一致。
        expo_url = expo_url_ready or extracted_expo_url

        return TaskResult(
            logs=logs,
            final_text=final_text,
            events=events,
            task_id=run_id,
            expo_root=expo_root,
            expo_url=expo_url,
        )
    finally:
        if workspace_token is not None:
            reset_task_workspace_root(workspace_token)

