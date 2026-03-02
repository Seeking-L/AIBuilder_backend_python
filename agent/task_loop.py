from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

from openai import OpenAI

from config import settings
from .system_prompt import get_system_prompt
from .tools import run_tool_call, tool_definitions


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
    # - 若调用方提供了 workspace_root_override，则优先使用；
    # - 否则退回到全局 settings.workspace_root。
    if input.workspace_root_override:
        effective_workspace_root = Path(input.workspace_root_override).expanduser().resolve()
    else:
        effective_workspace_root = settings.workspace_root

    system_prompt = get_system_prompt(
        workspace_root=effective_workspace_root,
        expo_root=Path(input.expo_root).resolve() if input.expo_root else None,
        task_id=input.task_id,
    )

    # 收集调试 / 展示用日志；会在 HTTP 接口返回给前端，方便用户查看每一轮行为。
    logs: List[str] = []
    # 对话上下文，按照 OpenAI chat 格式组织。
    # 这里只在一开始塞入一条 user 消息，后续每一轮会 append assistant / tool 消息。
    messages: List[ChatMessage] = [
        {
            "role": "user",
            "content": "\n".join(
                [
                    f"User request: {input.description}",
                    f"Preferred framework: {input.framework or 'expo'}",
                    f"Task id: {input.task_id or 'N/A'}",
                    f"Workspace root for this task: {effective_workspace_root}",
                    f"Expo app root for this task: {input.expo_root or '(not provided)'}",
                    "",
                    "Development constraints for this task:",
                    "- You must treat the Expo app root as a fixed template project copied from BaseCodeForAI/baseExpo.",
                    "- Only create or modify files under the Expo app root in these subdirectories: app/, components/common/, hooks/, services/, types/.",
                    "- Do NOT modify configuration or tooling files such as package.json, tsconfig.json, ESLint configs, or scripts (e.g. scripts/reset-project.js).",
                    "- Use Expo Router file-based routing for pages (e.g. app/profile/index.tsx, app/posts/[id].tsx).",
                    "- Use ScreenContainer, AppText, PrimaryButton, and Spacer from components/common for layout and UI where appropriate.",
                    "- Put network and data access logic into modules under services/; screens should not call fetch directly.",
                    "- Before running the Expo dev server, run `npm ci` in the Expo app root to install dependencies.",
                    "- Start the Expo dev server with `npm start` or `npx expo start --tunnel` using cwd set to the Expo app root.",
                    "",
                    "When planning your work, first outline which files you intend to add or modify (with paths relative to the Expo app root),",
                    "then use the available tools to actually write files and run commands.",
                ]
            ),
        }
    ]

    # 最终要返回给前端的自然语言总结（由最后一轮模型回复填充）
    final_text = ""

    # 限制最大轮数，避免模型或工具异常导致无限循环。
    for round_index in range(settings.max_task_rounds):
        logs.append(f"--- Round {round_index + 1} ---")

        # 发起一轮对话，传入当前累积的 messages 和可用工具定义
        assistant_text, tool_calls = api.create_message(
            system_prompt=system_prompt,
            messages=messages,
            tools=tool_definitions,
        )

        # 记录本轮 assistant 输出，便于在前端完整展示
        logs.append(f"Assistant:\n{assistant_text}")

        # 当模型没有再请求调用工具时，认为已经得到最终结果，可以提前结束循环
        if not tool_calls:
            final_text = assistant_text
            break

        # 保存每个工具调用的输出 / 错误文本，用于后续作为 tool 消息反馈给 LLM
        tool_results: List[str] = []

        # 依次执行每个工具调用，并把执行日志写入 logs
        for tool_call in tool_calls:
            logs.append(f"Tool call: {tool_call['function']['name']}")
            try:
                result = run_tool_call(tool_call)
                tool_results.append(
                    f"Tool {tool_call['function']['name']} (id={tool_call['id']}) result:\n{result}"
                )
            except Exception as exc:  # noqa: BLE001
                error_text = (
                    f"Tool {tool_call['function']['name']} (id={tool_call['id']}) error: {exc}"
                )
                tool_results.append(error_text)
                logs.append(error_text)

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

    # 在所有轮次结束后，尝试从日志与最终回复中提取 Expo URL
    joined_text = "\n".join(logs + [final_text])
    expo_url = _extract_expo_url(joined_text)

    return TaskResult(
        logs=logs,
        final_text=final_text,
        task_id=input.task_id,
        expo_root=input.expo_root,
        expo_url=expo_url,
    )

