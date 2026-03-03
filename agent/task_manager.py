from __future__ import annotations

import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Dict, List, Literal, Optional

from .events import AgentEvent

if TYPE_CHECKING:
    from .task_loop import TaskResult

# 后端控制台实时查看 AI 工作进度与命令输出的 logger
_progress_logger = logging.getLogger("agent.progress")


def _log_event(task_id: str, event: AgentEvent) -> None:
    """将事件以可读形式打到后端 logger，便于在控制台实时查看进度。"""
    tid = f"[{task_id[:8]}]" if task_id else ""
    if event.type == "command_start":
        _progress_logger.info("%s $ %s", tid, event.detail or "")
    elif event.type == "command_output":
        stream = event.title or "out"
        detail = (event.detail or "").rstrip("\n")
        if detail:
            _progress_logger.info("%s [%s] %s", tid, stream, detail)
    elif event.type == "command_end":
        _progress_logger.info("%s 命令结束 %s", tid, event.detail or "")
    elif event.type == "round_start":
        _progress_logger.info("%s %s", tid, event.title)
    elif event.type == "llm_response":
        _progress_logger.info("%s %s", tid, event.title)
    elif event.type == "tool_call":
        _progress_logger.info("%s %s", tid, event.title)
    elif event.type == "tool_result":
        _progress_logger.info("%s %s", tid, event.title)
    elif event.type == "finished":
        _progress_logger.info("%s %s", tid, event.title)


@dataclass
class TaskState:
    """后端内部维护的任务状态，用于 WebSocket / 轮询获取进度。"""

    task_id: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    description: Optional[str] = None
    framework: Optional[str] = None
    expo_root: Optional[str] = None
    events: List[AgentEvent] = field(default_factory=list)
    result: "TaskResult | None" = None
    error: Optional[str] = None
    # 简易 dev server 状态记录：当前任务下关联的 Expo dev server 信息（若有）。
    # 这里不直接持有进程句柄，只记录元数据，便于上层根据 task_id 查询。
    dev_server_port: Optional[int] = None
    dev_server_command: Optional[str] = None
    dev_server_pid: Optional[int] = None
    dev_server_status: Optional[Literal["starting", "running", "stopped", "failed"]] = None


class TaskManager:
    def __init__(self) -> None:
        self._tasks: Dict[str, TaskState] = {}
        self._lock = Lock()

    def init_task(
        self,
        task_id: str,
        description: Optional[str] = None,
        framework: Optional[str] = None,
        expo_root: Optional[str] = None,
    ) -> TaskState:
        """初始化一个任务状态（pending）。"""
        with self._lock:
            state = TaskState(
                task_id=task_id,
                status="pending",
                description=description,
                framework=framework,
                expo_root=expo_root,
            )
            self._tasks[task_id] = state
            return state

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                state = TaskState(task_id=task_id)
                self._tasks[task_id] = state
            state.status = "running"

    def append_event(self, task_id: str, event: AgentEvent) -> None:
        """向任务追加一条事件；若任务不存在则自动创建。"""
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                state = TaskState(task_id=task_id)
                self._tasks[task_id] = state
            state.events.append(event)
        # 在锁外打日志，避免 I/O 阻塞
        _log_event(task_id, event)

    def finish_task(self, task_id: str, result: "TaskResult") -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                state = TaskState(task_id=task_id)
                self._tasks[task_id] = state
            state.status = "completed"
            state.result = result
            # 若此前未记录事件，则直接用 result.events 兜底
            if not state.events:
                state.events = list(result.events)

    def fail_task(self, task_id: str, error: str) -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                state = TaskState(task_id=task_id)
                self._tasks[task_id] = state
            state.status = "failed"
            state.error = error

    def get_state(self, task_id: str) -> Optional[TaskState]:
        with self._lock:
            return self._tasks.get(task_id)

    # ---- Dev server registry helpers -------------------------------------------------

    def register_dev_server_start(
        self,
        task_id: str,
        *,
        port: Optional[int],
        command: str,
        pid: Optional[int] = None,
    ) -> None:
        """在任务状态中记录一次 Expo dev server 启动尝试。

        设计目标：
        - 让调用方可以在不直接保存子进程句柄的前提下，知道当前 task 下是否已经启动过 dev server；
        - 只存储元数据（端口 / 启动命令 / pid / 状态），便于前端或其它后端逻辑查询；
        - 不做强制约束（例如自动拒绝第二个 dev server），这一层只负责记录。
        """
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                state = TaskState(task_id=task_id)
                self._tasks[task_id] = state
            state.dev_server_port = port
            state.dev_server_command = command
            state.dev_server_pid = pid
            state.dev_server_status = "starting"

    def register_dev_server_running(self, task_id: str) -> None:
        """将当前任务下的 dev server 状态标记为 running（例如命令已成功进入等待请求阶段）。"""
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return
            # 仅在已存在 dev server 记录时更新状态
            if state.dev_server_status in {"starting", None}:
                state.dev_server_status = "running"

    def register_dev_server_stopped(
        self,
        task_id: str,
        *,
        failed: bool = False,
    ) -> None:
        """在命令结束时记录 dev server 已停止。

        注意：这里不尝试区分“自然退出”与“被超时/信号杀死”的场景，
        那些信息已经体现在事件与命令输出中；本方法只关心最终是否还在运行。
        """
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return
            # 只有当之前确实记录过 dev server 时才更新状态
            if state.dev_server_status is not None:
                state.dev_server_status = "failed" if failed else "stopped"


task_manager = TaskManager()

