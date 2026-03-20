from __future__ import annotations

import asyncio
import json
import threading
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi import WebSocket

from auth.session import SESSION_COOKIE, get_session_id_from_websocket
from config import settings
from models import (
    CreateConversationResponse,
    ListConversationsResponse,
    ConversationItem,
    ConversationMessagesResponse,
    ConversationMessage,
    SendMessageRequest,
    SendMessageResponse,
)
from agent.task_loop import ChatMessage, ToolCall, run_conversation_turn
from agent.summary_memory import build_summary_input_text, generate_conversation_summary
from storage.sqlite_store import SqliteStore
from workspace.conversation_workspace import prepare_conversation_workspace


router = APIRouter(prefix="/conversations", tags=["conversations"])

# 这里为了 MVP 简化：模块级别创建 store 实例。
# 后续可按 db-migration-init-on-startup todo 改为 app.state 注入方式。
store = SqliteStore(settings.db_path)

_conversation_locks: dict[str, threading.Lock] = {}


def _get_conversation_lock(conversation_id: str) -> threading.Lock:
    """同一 conversation_id 串行执行 run，避免并发写同一份 workspace。"""

    lock = _conversation_locks.get(conversation_id)
    if lock is None:
        lock = threading.Lock()
        _conversation_locks[conversation_id] = lock
    return lock


def _require_session_id(request: Request) -> str:
    # middleware 已经会写入 cookie，但这里仍做兜底校验
    sid = request.cookies.get(SESSION_COOKIE.cookie_name)
    if not sid:
        raise HTTPException(status_code=401, detail="Missing session cookie")
    return sid


@router.post("", response_model=CreateConversationResponse)
def create_conversation(request: Request) -> CreateConversationResponse:
    session_id = _require_session_id(request)
    store.touch_session(session_id)

    conversation_id = uuid4().hex
    workspace_root, expo_root = prepare_conversation_workspace(conversation_id)

    # 初始 title 可为空，后续在发送首条消息后用 content 生成
    store.create_conversation(
        session_id=session_id,
        conversation_id=conversation_id,
        title=None,
        workspace_root=workspace_root,
        expo_root=expo_root,
    )

    return CreateConversationResponse(
        conversationId=conversation_id,
        title=None,
        expoRoot=expo_root,
    )


@router.get("", response_model=ListConversationsResponse)
def list_conversations(request: Request) -> ListConversationsResponse:
    session_id = _require_session_id(request)
    store.touch_session(session_id)

    rows = store.list_conversations(session_id=session_id)
    items: list[ConversationItem] = []
    for r in rows:
        items.append(
            ConversationItem(
                conversationId=r["conversation_id"],
                title=r.get("title"),
                createdAt=r["created_at"],
                updatedAt=r["updated_at"],
            )
        )
    return ListConversationsResponse(conversations=items)


@router.get("/{conversation_id}", response_model=CreateConversationResponse)
def get_conversation_detail(
    request: Request,
    conversation_id: str,
) -> CreateConversationResponse:
    session_id = _require_session_id(request)
    store.touch_session(session_id)

    conv = store.get_conversation_or_none(
        session_id=session_id, conversation_id=conversation_id
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return CreateConversationResponse(
        conversationId=conv["conversation_id"],
        title=conv.get("title"),
        expoRoot=conv["expo_root"],
    )


@router.get("/{conversation_id}/messages", response_model=ConversationMessagesResponse)
def get_conversation_messages(
    request: Request,
    conversation_id: str,
) -> ConversationMessagesResponse:
    session_id = _require_session_id(request)
    store.touch_session(session_id)

    conv = store.get_conversation_or_none(
        session_id=session_id, conversation_id=conversation_id
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs = store.list_messages(conversation_id=conversation_id)
    ui_messages: list[ConversationMessage] = []
    for m in msgs:
        ui_messages.append(
            ConversationMessage(
                role=m["role"],
                content=m["content"],
                toolCallId=m.get("tool_call_id"),
            )
        )

    return ConversationMessagesResponse(
        conversationId=conversation_id,
        messages=ui_messages,
        title=conv.get("title"),
    )


def _convert_message_rows_to_openai_messages(rows: list[dict[str, object]]) -> list[ChatMessage]:
    """把 messages 表的行记录转换为 agent 所需的 OpenAI messages 结构。"""

    result: list[ChatMessage] = []
    for m in rows:
        # typed: store 返回 dict[str, Any]，这里按运行时字段访问
        role = m["role"]
        content = m["content"]  # type: ignore[assignment]

        if role == "assistant":
            tool_calls_json = m.get("tool_calls_json")
            if tool_calls_json:
                tool_calls = json.loads(tool_calls_json)
                result.append(
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls,
                    }
                )
            else:
                result.append(
                    {
                        "role": "assistant",
                        "content": content,
                    }
                )
        elif role == "tool":
            result.append(
                {
                    "role": "tool",
                    "content": content,
                    "tool_call_id": (m.get("tool_call_id") or ""),  # type: ignore[union-attr]
                }
            )
        else:
            # user / system（当前系统只保存 user/assistant/tool）
            result.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    return result


def _should_update_summary(*, message_rows: list[dict[str, object]]) -> bool:
    total_chars = 0
    for r in message_rows:
        content = r.get("content") or ""
        total_chars += len(str(content))
    return (
        total_chars > settings.summary_trigger_chars
        or len(message_rows) > settings.summary_trigger_messages
    )


def _select_recent_messages_for_model(
    *, message_rows: list[dict[str, object]]
) -> list[dict[str, object]]:
    """为“当前 run 构造 messages_for_model”选择最近片段。

    关键点：尽量从最近的 assistant 边界开始截断，避免把 tool messages 裁掉导致 OpenAI tool_call_id 不匹配。
    """

    assistant_indices = [
        i for i, r in enumerate(message_rows) if r.get("role") == "assistant"
    ]
    if not assistant_indices:
        # 没有 assistant 历史（通常是首次对话），直接按尾部消息截断
        tail_n = max(1, settings.recent_messages_for_model)
        return message_rows[-tail_n:]

    keep_n = max(1, settings.recent_messages_for_model)
    selected = assistant_indices[-keep_n:]
    start_index = selected[0]
    return message_rows[start_index:]


def _background_run_conversation_turn(
    *,
    conversation_id: str,
    run_id: str,
    workspace_root: str,
    expo_root: str,
) -> None:
    """后台执行一次 conversation run（方案 A：一次用户消息触发一次 run）。"""

    lock = _get_conversation_lock(conversation_id)
    with lock:
        try:
            message_rows = store.list_messages(conversation_id=conversation_id)

            # 1) 超长历史：更新 summary memory（但仍保存全部历史用于 UI / 审计）
            if _should_update_summary(message_rows=message_rows):
                existing_summary = store.get_conversation_summary_or_none(
                    conversation_id=conversation_id
                )
                existing_summary_text = (
                    existing_summary.get("summary_text") if existing_summary else None
                )
                try:
                    summary_input_text = build_summary_input_text(
                        messages_rows=message_rows,
                        existing_summary_text=existing_summary_text,
                        head_messages=20,
                        tail_messages=20,
                        max_chars=9000,
                    )
                    new_summary = generate_conversation_summary(
                        summary_input_text=summary_input_text
                    )
                    if new_summary:
                        store.upsert_conversation_summary(
                            conversation_id=conversation_id,
                            summary_text=new_summary,
                        )
                except Exception as exc:  # noqa: BLE001
                    # 摘要失败时降级：不影响本次 run 正常执行
                    print("[summary_memory] failed:", repr(exc))

            # 2) 构造 messages_for_model：summary + 最近 N 条消息
            summary_row = store.get_conversation_summary_or_none(
                conversation_id=conversation_id
            )
            summary_text = summary_row.get("summary_text") if summary_row else None

            recent_rows = _select_recent_messages_for_model(
                message_rows=message_rows
            )
            existing_messages = _convert_message_rows_to_openai_messages(
                recent_rows
            )

            if summary_text:
                # 把 summary 插入到 system 层，确保与系统约束并列出现
                existing_messages = [
                    {"role": "system", "content": summary_text},
                    *existing_messages,
                ]

            def event_sink(ev):
                store.append_agent_event(
                    run_id=run_id,
                    step_id=ev.step_id,
                    type=ev.type,
                    title=ev.title,
                    detail=ev.detail,
                )

            def persist_message(
                role: str,
                content: str,
                tool_call_id: str | None,
                tool_calls: list[ToolCall] | None,
            ) -> None:
                store.append_message(
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    tool_call_id=tool_call_id,
                    tool_calls=tool_calls,
                )

            result = run_conversation_turn(
                run_id=run_id,
                workspace_root_override=workspace_root,
                expo_root=expo_root,
                existing_messages=existing_messages,
                event_sink=event_sink,
                persist_message=persist_message,
            )

            store.set_run_status(
                run_id=run_id,
                status=store.status.completed,
                error=None,
                final_text=result.final_text,
                expo_url=result.expo_url,
            )
        except Exception as exc:  # noqa: BLE001
            store.set_run_status(
                run_id=run_id,
                status=store.status.failed,
                error=str(exc),
            )


@router.post("/{conversation_id}/messages", response_model=SendMessageResponse)
def send_message(
    request: Request,
    conversation_id: str,
    payload: SendMessageRequest,
    background_tasks: BackgroundTasks,
) -> SendMessageResponse:
    session_id = _require_session_id(request)
    store.touch_session(session_id)

    conv = store.get_conversation_or_none(
        session_id=session_id, conversation_id=conversation_id
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # 对同一个 conversation 串行：若已存在 running run，则拒绝本次请求（409）。
    with _get_conversation_lock(conversation_id):
        running = store.get_running_run_for_conversation_or_none(
            conversation_id=conversation_id
        )
        if running is not None:
            raise HTTPException(
                status_code=409,
                detail="A run is already running for this conversation",
            )

        # 首轮消息生成 conversation.title（满足“刷新后能显示窗口标题”）
        if not conv.get("title"):
            title = (payload.text or "").strip()
            if len(title) > 60:
                title = title[:60] + "..."
            store.set_conversation_title(
                conversation_id=conversation_id,
                title=title,
            )

        # 先把用户消息落库：无论 run 最终是否成功，刷新时都能看到“用户已发送”
        store.append_message(
            conversation_id=conversation_id,
            role="user",
            content=payload.text,
        )
        store.touch_conversation(conversation_id=conversation_id)

        # 为本次“用户消息触发的 run”创建 run 记录
        run_id = store.create_run(
            conversation_id=conversation_id,
            input_text=payload.text,
            status=store.status.running,
        )

    background_tasks.add_task(
        _background_run_conversation_turn,
        conversation_id=conversation_id,
        run_id=run_id,
        workspace_root=conv["workspace_root"],
        expo_root=conv["expo_root"],
    )

    return SendMessageResponse(runId=run_id)


@router.websocket("/ws/{conversation_id}/{run_id}")
async def conversation_events_ws(
    websocket: WebSocket,
    conversation_id: str,
    run_id: str,
) -> None:
    """WebSocket：按 runId 推送该 conversation 的 AgentEvent 增量。"""

    await websocket.accept()

    session_id = get_session_id_from_websocket(websocket)
    if not session_id:
        # 约定：没 cookie 就直接拒绝
        await websocket.send_json(
            {"type": "task_status", "status": "failed", "error": "Missing session cookie"}
        )
        await websocket.close()
        return

    conv = store.get_conversation_or_none(
        session_id=session_id, conversation_id=conversation_id
    )
    if conv is None:
        await websocket.send_json(
            {"type": "task_status", "status": "failed", "error": "Conversation not found"}
        )
        await websocket.close()
        return

    # lastStepId：客户端重连时用来拉取增量
    last_step_id_raw = websocket.query_params.get("lastStepId") or "0"
    try:
        last_step_id = int(last_step_id_raw)
    except ValueError:
        last_step_id = 0

    # lastEventSeq：客户端重连时用来精确拉取同一步骤内的增量事件
    # （向后兼容：若前端没有传该参数，则以 step_id 做粗粒度增量。）
    last_event_seq_raw = websocket.query_params.get("lastEventSeq")
    last_event_seq: int | None = None
    if last_event_seq_raw is not None:
        try:
            last_event_seq = int(last_event_seq_raw)
        except ValueError:
            last_event_seq = None
    else:
        # 向后兼容：
        # 如果前端没有提供 lastEventSeq，则无法精确判断同一步骤内已经送达了哪条事件。
        # 为了避免“漏掉同一步骤内 command_output/stdin”等事件，这里保守地取 -1，
        # 重连时会补发该 step_id 之后（包含 step 内所有事件）的增量。
        last_event_seq = -1

    while True:
        try:
            new_events = store.list_agent_events_incremental(
                run_id=run_id,
                last_step_id=last_step_id,
                last_event_seq=last_event_seq,
            )
        except Exception:
            new_events = []

        for evt in new_events:
            last_step_id = int(evt["step_id"])
            # event_seq 字段用于同 step 内精确增量
            if "event_seq" in evt and evt["event_seq"] is not None:
                last_event_seq = int(evt["event_seq"])
            await websocket.send_json(
                {
                    "stepId": evt["step_id"],
                    "type": evt["type"],
                    "title": evt["title"],
                    "detail": evt["detail"],
                    "eventSeq": evt.get("event_seq"),
                }
            )

        run = store.get_run_or_none(run_id=run_id)
        if run is not None and run["status"] in (store.status.completed, store.status.failed):
            if run.get("conversation_id") != conversation_id:
                await websocket.send_json(
                    {
                        "type": "task_status",
                        "status": "failed",
                        "error": "Run does not belong to conversation",
                    }
                )
                await websocket.close()
                break
            # 没有新事件就推送一次最终状态并关闭连接
            if not new_events:
                expo_url = run.get("expo_url") if run is not None else None
                await websocket.send_json(
                    {
                        "type": "task_status",
                        "status": run["status"],
                        "error": run["error"],
                        # 完成时提供给前端“查看应用”按钮的跳转链接
                        "expoUrl": expo_url if run["status"] == store.status.completed else None,
                    }
                )
                await websocket.close()
                break

        await asyncio.sleep(0.5)

