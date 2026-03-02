from __future__ import annotations

from typing import Any, Dict, List, Tuple

from openai import OpenAI

from config import settings

ChatMessage = Dict[str, Any]
ToolDefinition = Dict[str, Any]
ToolCall = Dict[str, Any]


class ApiHandler:
    """LLM API 封装（OpenAI 兼容：OpenAI / Kimi 等）。

    该版本是一个相对精简的封装，用于更简单的调用场景：
    - 只负责「发一轮消息 + 可选工具调用」；
    - 由上层负责循环、多轮对话和工具结果回传。
    """

    def __init__(self) -> None:
        # 根据配置选择使用的模型服务提供方（OpenAI / Kimi 等）
        provider = settings.model_provider

        if provider == "openai":
            # OpenAI 模式必须提供 OPENAI_API_KEY
            if not settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is required when MODEL_PROVIDER=openai")
            self._client = OpenAI(api_key=settings.openai_api_key)
        elif provider == "kimi":
            # Kimi 模式必须提供 KIMI_API_KEY 或 MOONSHOT_API_KEY
            if not settings.kimi_api_key:
                raise RuntimeError("KIMI_API_KEY or MOONSHOT_API_KEY is required when MODEL_PROVIDER=kimi")
            # Kimi 使用 OpenAI 兼容协议，通过 base_url 指向 Moonshot 平台
            self._client = OpenAI(
                api_key=settings.kimi_api_key,
                base_url="https://api.moonshot.cn/v1",
            )
        elif provider == "qwen":
            # 千问（DashScope）模式必须提供 DASHSCOPE_API_KEY
            if not settings.qwen_api_key:
                raise RuntimeError("DASHSCOPE_API_KEY is required when MODEL_PROVIDER=qwen")
            # 千问提供 OpenAI 兼容接口，通过 base_url 指向 DashScope 平台
            self._client = OpenAI(
                api_key=settings.qwen_api_key,
                base_url=settings.dashscope_base_url,
            )
        else:
            raise RuntimeError(f"Unsupported model provider: {provider}")

    def create_message(
        self,
        system_prompt: str,
        messages: List[ChatMessage],
        tools: List[ToolDefinition] | None = None,
    ) -> Tuple[str, List[ToolCall]]:
        """发送一轮对话并返回 (assistant_text, tool_calls)。"""
        full_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}, *messages]

        response = self._client.chat.completions.create(
            model=settings.model_name,
            messages=full_messages,
            tools=tools or [],
        )

        choice = response.choices[0]
        message = choice.message

        assistant_text = message.content or ""

        tool_calls: List[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                if tc.type != "function":
                    continue
                tool_calls.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )

        return assistant_text, tool_calls

