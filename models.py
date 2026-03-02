from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class GenerateAppRequest(BaseModel):
    description: str = Field(..., min_length=1)
    framework: Optional[str] = "expo"


class GenerateAppResponse(BaseModel):
    status: Literal["completed"]
    description: str
    framework: str
    # Agent 整个执行过程中的日志，
    # 包含每一轮模型回复与工具调用记录，方便前端展示调试信息。
    logs: List[str]
    # LLM 产出的最终总结性文本，一般用于前端结果区展示。
    summary: str
    # 本次任务 ID，便于前端和后端排查问题。
    taskId: str
    # 本次生成的 Expo 应用根目录（通常形如 generated/<task-id>/baseExpo，相对于 WORKSPACE_ROOT）。
    expoRoot: str
    # 尝试从执行日志中提取到的 Expo URL（如 exp://... 或 http://localhost:...），前端可据此生成二维码。
    expoUrl: Optional[str] = None

