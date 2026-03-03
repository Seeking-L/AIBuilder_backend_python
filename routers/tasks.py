from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from config import settings
from models import (
    AgentEvent as AgentEventModel,
    GenerateAppRequest,
    GenerateAppResponse,
    StartGenerateAppResponse,
)
from agent.task_loop import TaskInput, TaskResult, run_task_loop
from agent.task_manager import task_manager


router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("/generate-app", response_model=GenerateAppResponse)
def generate_app(payload: GenerateAppRequest) -> GenerateAppResponse | JSONResponse:
    # 从请求体中取出描述文案，并做一次去空格处理
    description = (payload.description or "").strip()
    if not description:
        # 与 TypeScript 版本保持一致的 400 返回结构：
        # - 使用 JSONResponse 显式设置状态码；
        # - 返回一个简单的 error 字段，方便前端统一处理。
        return JSONResponse(status_code=400, content={"error": "description is required"})

    # 为本次任务生成唯一 ID，并在工作区下创建对应的 generated/<task-id>/ 目录
    task_id = uuid4().hex
    task_workspace_root = settings.workspace_root / "generated" / task_id
    task_workspace_root.mkdir(parents=True, exist_ok=True)

    # 计算后端模板 baseExpo 的物理路径（位于当前后端项目的 BaseCodeForAI/baseExpo）
    backend_root = Path(__file__).resolve().parent.parent
    template_expo_root = backend_root / "BaseCodeForAI" / "baseExpo"

    # 将模板拷贝到本次任务的工作区中：generated/<task-id>/baseExpo
    expo_root = task_workspace_root / "baseExpo"
    if expo_root.exists():
        # 理论上新任务不会存在同名目录，这里做一次兜底清理
        shutil.rmtree(expo_root)
    shutil.copytree(
        template_expo_root,
        expo_root,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("node_modules"),
    )

    # 将请求参数转换为后端 Agent 所需的 TaskInput 结构，并附带任务上下文
    task_input = TaskInput(
        description=description,
        framework=payload.framework,
        task_id=task_id,
        workspace_root_override=str(task_workspace_root),
        expo_root=str(expo_root),
    )
    # 调用多轮「LLM + 工具调用」任务循环，直到拿到最终结果或达到轮数上限
    result: TaskResult = run_task_loop(task_input)

    # 将内部的 AgentEvent 转成前端使用的 Pydantic 模型
    api_events = [
        AgentEventModel(
            stepId=evt.step_id,
            type=evt.type,
            title=evt.title,
            detail=evt.detail,
        )
        for evt in getattr(result, "events", []) or []
    ]

    # 按照约定的响应模型组装返回结果：
    # - status：当前实现中固定为 "completed"；
    # - logs：包含每一轮对话与工具调用日志，方便前端展示；
    # - summary：由 LLM 产出的最终总结文案。
    return GenerateAppResponse(
        status="completed",
        description=description,
        framework=payload.framework or "expo",
        logs=result.logs,
        summary=result.final_text,
        taskId=result.task_id or task_id,
        expoRoot=result.expo_root or str(expo_root),
        expoUrl=result.expo_url,
        events=api_events,
    )


def _run_task_in_background(task_input: TaskInput) -> None:
    """后台任务：执行多轮 Agent 逻辑，并把最终结果写入 TaskManager。"""
    task_id = task_input.task_id or ""
    if task_id:
        task_manager.mark_running(task_id)
    try:
        result = run_task_loop(task_input)
        if task_id:
            task_manager.finish_task(task_id, result)
    except Exception as exc:  # noqa: BLE001
        if task_id:
            task_manager.fail_task(task_id, str(exc))


@router.post("/generate-app-async", response_model=StartGenerateAppResponse)
def generate_app_async(
    payload: GenerateAppRequest,
    background_tasks: BackgroundTasks,
) -> StartGenerateAppResponse | JSONResponse:
    """异步启动应用生成任务，配合 WebSocket 实时查看进度。"""
    description = (payload.description or "").strip()
    if not description:
        return JSONResponse(status_code=400, content={"error": "description is required"})

    # 为本次任务生成唯一 ID，并在工作区下创建对应的 generated/<task-id>/ 目录
    task_id = uuid4().hex
    task_workspace_root = settings.workspace_root / "generated" / task_id
    task_workspace_root.mkdir(parents=True, exist_ok=True)

    # 计算后端模板 baseExpo 的物理路径（位于当前后端项目的 BaseCodeForAI/baseExpo）
    backend_root = Path(__file__).resolve().parent.parent
    template_expo_root = backend_root / "BaseCodeForAI" / "baseExpo"

    # 将模板拷贝到本次任务的工作区中：generated/<task-id>/baseExpo
    expo_root = task_workspace_root / "baseExpo"
    if expo_root.exists():
        shutil.rmtree(expo_root)
    shutil.copytree(
        template_expo_root,
        expo_root,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("node_modules"),
    )

    # 初始化 TaskManager 状态（pending）
    task_manager.init_task(
        task_id=task_id,
        description=description,
        framework=payload.framework or "expo",
        expo_root=str(expo_root),
    )

    # 组装 TaskInput，丢到后台任务中执行
    task_input = TaskInput(
        description=description,
        framework=payload.framework,
        task_id=task_id,
        workspace_root_override=str(task_workspace_root),
        expo_root=str(expo_root),
    )
    background_tasks.add_task(_run_task_in_background, task_input)

    return StartGenerateAppResponse(
        status="accepted",
        taskId=task_id,
        expoRoot=str(expo_root),
    )


@router.websocket("/ws/{task_id}")
async def task_events_ws(websocket: WebSocket, task_id: str) -> None:
    """WebSocket：按时间顺序实时推送任务过程事件。"""
    await websocket.accept()
    last_index = 0

    try:
        while True:
            state = task_manager.get_state(task_id)

            # 若任务尚未初始化，则等待一会儿
            if state is None:
                await asyncio.sleep(0.3)
                continue

            events = list(state.events)
            if last_index < len(events):
                new_events = events[last_index:]
                last_index = len(events)
                for evt in new_events:
                    await websocket.send_json(
                        {
                            "stepId": evt.step_id,
                            "type": evt.type,
                            "title": evt.title,
                            "detail": evt.detail,
                        }
                    )

            # 若任务已结束且没有新的事件，则发送一次状态并关闭连接
            if state.status in ("completed", "failed") and last_index >= len(events):
                await websocket.send_json(
                    {
                        "type": "task_status",
                        "status": state.status,
                        "error": state.error,
                    }
                )
                await websocket.close()
                break

            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        # 客户端主动断开，无需额外处理
        return

