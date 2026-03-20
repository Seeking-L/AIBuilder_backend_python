from __future__ import annotations

import json
from typing import Any, Optional

from openai import OpenAI

from config import settings


def _create_openai_client() -> OpenAI:
    """创建 OpenAI 兼容客户端（与 agent.task_loop.ApiHandler 保持一致策略）。"""

    provider = settings.model_provider
    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required when MODEL_PROVIDER=openai")
        return OpenAI(api_key=settings.openai_api_key)

    if provider == "kimi":
        if not settings.kimi_api_key:
            raise RuntimeError(
                "KIMI_API_KEY or MOONSHOT_API_KEY is required when MODEL_PROVIDER=kimi"
            )
        return OpenAI(api_key=settings.kimi_api_key, base_url="https://api.moonshot.cn/v1")

    if provider == "qwen":
        if not settings.qwen_api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required when MODEL_PROVIDER=qwen")
        return OpenAI(api_key=settings.qwen_api_key, base_url=settings.dashscope_base_url)

    raise RuntimeError(f"Unsupported model provider: {provider}")


def build_summary_input_text(
    *,
    messages_rows: list[dict[str, Any]],
    existing_summary_text: Optional[str],
    head_messages: int = 20,
    tail_messages: int = 20,
    max_chars: int = 8000,
) -> str:
    """把“需要用于生成摘要”的对话片段拼成一个文本输入。"""

    head = messages_rows[:head_messages]
    tail = messages_rows[-tail_messages:] if tail_messages > 0 else []

    def _format_one(m: dict[str, Any]) -> str:
        role = m.get("role") or "unknown"
        content = m.get("content") or ""
        # 防止单条工具输出过长导致 summary prompt 爆炸
        if len(content) > 1800:
            content = content[:1800] + "...(truncated)"
        return f"{role.upper()}:\n{content}"

    parts: list[str] = []
    if existing_summary_text:
        parts.append("EXISTING_SUMMARY:\n" + existing_summary_text)

    parts.append("CONVERSATION_HEAD:\n" + "\n\n".join(_format_one(m) for m in head))
    if tail:
        parts.append("CONVERSATION_TAIL:\n" + "\n\n".join(_format_one(m) for m in tail))

    text = "\n\n".join(p for p in parts if p)
    if len(text) > max_chars:
        text = text[:max_chars] + "...(truncated)"
    return text


def generate_conversation_summary(*, summary_input_text: str) -> str:
    """调用大模型生成 summary 文本。"""

    client = _create_openai_client()

    system_prompt = (
        "你是一个对话摘要器，负责把“面向 AI 编程代理的多轮对话 + 工程修改意图”压缩成短摘要。\n"
        "要求：\n"
        "- 摘要必须覆盖：用户目标、已完成的关键改动（按模块/文件/功能概述）、当前应用状态、未完成任务、约束与偏好。\n"
        "- 使用中文，尽量具体但要简洁。\n"
        "- 输出不要包含推理过程，只输出摘要正文。\n"
        "- 不要编造对话中从未出现的事实；不确定就写“未明确”。"
    )

    response = client.chat.completions.create(
        model=settings.model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": summary_input_text},
        ],
        temperature=0.2,
    )

    choice = response.choices[0]
    content = choice.message.content or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return content.strip()

