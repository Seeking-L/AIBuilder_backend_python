from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass
class AgentEvent:
    """单条 Agent 过程事件，用于前端展示 AI 的工作步骤。"""

    step_id: int
    type: Literal[
        "round_start",
        "llm_response",
        "tool_call",
        "tool_result",
        "finished",
        "command_start",
        "command_output",
        "command_end",
        # 当模型通过 `notify_expo_url_ready` tool 成功通知后端时发出：
        # 前端可展示“查看应用”按钮，并根据 detail 中的 exp://... 打开 Expo Go。
        "expo_url_ready",
    ]
    title: str
    detail: Optional[str] = None

