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
    ]
    title: str
    detail: Optional[str] = None

