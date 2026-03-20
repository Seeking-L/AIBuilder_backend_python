from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class GenerateAppRequest(BaseModel):
    description: str = Field(..., min_length=1)
    framework: Optional[str] = "expo"


class AgentEvent(BaseModel):
    """前端展示用的 Agent 过程事件。"""

    stepId: int
    type: Literal[
        "round_start",
        "llm_response",
        "tool_call",
        "tool_result",
        "finished",
        "command_start",
        "command_output",
        "command_end",
        # AI 生成结束后调用 notify_expo_url_ready tool，
        # 后端把 expoUrl 通过此事件通知前端。
        "expo_url_ready",
    ]
    title: str
    detail: Optional[str] = None


class StartGenerateAppResponse(BaseModel):
    """异步生成应用：任务已接受时的返回结构。"""

    status: Literal["accepted"]
    taskId: str
    # 本次生成的 Expo 根目录（便于前端在任务未完成前就知道路径）
    expoRoot: str


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
    # 结构化的过程事件列表，前端可按时间线展示 AI 的工作过程。
    events: List[AgentEvent] = Field(default_factory=list)


# =============================
# Conversation (窗口/多轮聊天)
# =============================


class ConversationItem(BaseModel):
    conversationId: str
    title: Optional[str] = None
    createdAt: int
    updatedAt: int


class CreateConversationResponse(BaseModel):
    status: Literal["created"] = "created"
    conversationId: str
    title: Optional[str] = None
    expoRoot: str


class ListConversationsResponse(BaseModel):
    conversations: List[ConversationItem] = Field(default_factory=list)


class ConversationMessage(BaseModel):
    """给前端渲染用的消息结构（聊天气泡）。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    toolCallId: Optional[str] = None


class ConversationMessagesResponse(BaseModel):
    conversationId: str
    messages: List[ConversationMessage] = Field(default_factory=list)
    title: Optional[str] = None


class SendMessageRequest(BaseModel):
    text: str = Field(..., min_length=1)
    framework: Optional[str] = "expo"
    # 可选：用于当你允许“手动指定窗口目标/标题”时扩展
    optionalTitle: Optional[str] = None


class SendMessageResponse(BaseModel):
    status: Literal["accepted"] = "accepted"
    runId: str


