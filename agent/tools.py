from __future__ import annotations

import json
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, MutableMapping, Optional
from contextvars import ContextVar

from config import settings


ToolDefinition = Dict[str, Any]


# 当前任务的 workspace 根目录（通常为 AIBuilder_workspace/generated/<task-id>），
# 默认退回到全局 settings.workspace_root。
_CURRENT_WORKSPACE_ROOT: ContextVar[Path] = ContextVar(
    "CURRENT_WORKSPACE_ROOT",
    default=settings.workspace_root.resolve(),
)


def get_effective_workspace_root() -> Path:
    """获取当前生效的 workspace 根目录（按任务可覆盖）。"""
    return _CURRENT_WORKSPACE_ROOT.get()


def set_task_workspace_root(root: Path) -> object:
    """为当前任务设置 workspace 根目录，上下文敏感（基于 ContextVar）。"""
    return _CURRENT_WORKSPACE_ROOT.set(root.resolve())


def reset_task_workspace_root(token: object) -> None:
    """重置当前任务 workspace 根目录到之前的值。"""
    _CURRENT_WORKSPACE_ROOT.reset(token)


TOOL_DEFINITIONS: List[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            # 描述：让 LLM 可以在后端工作区内执行一条 shell 命令
            "description": "Execute a shell command in the project workspace (for tests, builds, etc).",
            "parameters": {
                "type": "object",
                "properties": {
                    # 要执行的命令，例如：`npm test`、`pytest` 等
                    "command": {
                        "type": "string",
                        "description": "The shell command to run (e.g. npm test, npm run build).",
                    },
                    # 可选：相对于 workspace_root 的工作目录
                    "cwd": {
                        "type": "string",
                        "description": "Optional relative working directory inside the workspace.",
                    },
                    # 可选：超时时间（秒），避免命令长时间挂起
                    "timeoutSeconds": {
                        "type": "number",
                        "description": "Optional timeout in seconds for the command.",
                    },
                    # 可选：标记为长时间运行的命令（如 dev server），禁用超时自动终止
                    "longRunning": {
                        "type": "boolean",
                        "description": "Set true for long-running dev servers; disables timeout-based termination.",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_to_file",
            # 描述：让 LLM 可以在工作区内创建 / 覆盖文件内容
            "description": "Create or overwrite a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    # 相对工作区根目录的文件路径
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file within the workspace.",
                    },
                    # 要写入文件的完整文本内容
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_available_port",
            "description": "Get a single TCP port that is currently free on this machine. Call this before starting a dev server (e.g. Expo/Metro) so you can pass --port <port> and avoid 8080/8081 being in use.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minPort": {
                        "type": "number",
                        "description": "Optional. Start searching from this port (default 8080). First free port from minPort up to 65535 is returned.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]

# 为兼容旧代码，提供小写别名。
# 其它模块可以继续从 `tool_definitions` 导入，而不关心常量名是否变化。
tool_definitions: List[ToolDefinition] = TOOL_DEFINITIONS


def _ensure_within_workspace(path: Path) -> Path:
    """确保目标路径在当前任务的 workspace 根目录内，并且不指向共享 baseExpo 模板。

    - 第一层：必须在当前生效的 workspace_root（通常是 generated/<task-id>）内部；
    - 第二层：显式禁止指向共享的 baseExpo 模板目录，例如：
      - 后端仓库中的 `BaseCodeForAI/baseExpo`；
      - 全局工作区根下的 `AIBuilder_workspace/baseExpo`（如果存在）。
    """
    base = get_effective_workspace_root().resolve()
    target = path.resolve()

    # 不允许逃逸出当前任务 workspace 根目录
    if not str(target).startswith(str(base)):
        raise ValueError("Target path escapes workspace root for this task")

    # 显式禁止写入/在共享 baseExpo 模板下执行命令
    backend_root = Path(__file__).resolve().parent.parent
    shared_template_expo = (backend_root / "BaseCodeForAI" / "baseExpo").resolve()
    shared_workspace_expo = (settings.workspace_root / "baseExpo").resolve()

    if str(target).startswith(str(shared_template_expo)) or str(target).startswith(
        str(shared_workspace_expo)
    ):
        raise ValueError(
            "Target path points to shared baseExpo template; use the per-task generated/<task-id>/baseExpo instead."
        )

    return target


def _read_stream(
    pipe: Any,
    stream_name: Literal["stdout", "stderr"],
    parts: List[str],
    on_output: Optional[Callable[[str, Literal["stdout", "stderr"]], None]],
) -> None:
    """在单独线程中读取管道，按行回调并收集到 parts。"""
    if pipe is None:
        return
    try:
        for line in iter(pipe.readline, ""):
            parts.append(line)
            if on_output:
                on_output(line, stream_name)
    except (ValueError, OSError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def execute_command_tool(
    args: Mapping[str, Any],
    *,
    on_command_start: Optional[Callable[[str], None]] = None,
    on_command_output: Optional[Callable[[str, Literal["stdout", "stderr"]], None]] = None,
    on_command_end: Optional[Callable[[int, str, str], None]] = None,
) -> str:
    # 从 tool 调用参数中解析出要执行的命令；若缺失则报错
    command = str(args.get("command") or "").strip()
    if not command:
        raise ValueError("`command` is required")

    # 可选工作目录（相对路径），以及可选超时时间
    cwd_arg = args.get("cwd")
    timeout_seconds = args.get("timeoutSeconds")
    long_running = bool(args.get("longRunning"))
    timeout_float: Optional[float] = (
        float(timeout_seconds)
        if (timeout_seconds is not None and not long_running)
        else None
    )

    # 所有命令都必须在当前任务的 workspace_root 内执行
    base = get_effective_workspace_root().resolve()
    if cwd_arg:
        cwd_path = _ensure_within_workspace(base / str(cwd_arg))
    else:
        cwd_path = _ensure_within_workspace(base)

    use_streaming = (
        on_command_start is not None
        or on_command_output is not None
        or on_command_end is not None
    )

    if not use_streaming:
        # 无回调时保持原有 subprocess.run 行为
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_float,
            )
        except subprocess.TimeoutExpired as exc:
            return (
                f"[execute_command] Timeout after {exc.timeout} seconds while running: "
                f"{command}"
            )
        except Exception as exc:
            return f"[execute_command] Failed to run `{command}`: {exc}"

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        prefix = (
            "[execute_command] Long-running command"
            if long_running
            else "[execute_command] Command"
        )
        notes: List[str] = []
        if completed.returncode == -1:
            notes.append(
                "Exit code -1 usually means the process was killed by a timeout or external signal."
            )
        if long_running:
            notes.append(
                "This command was marked as long-running; it is expected to keep running until it is stopped externally."
            )
        notes_text = ("\n".join(notes) + "\n") if notes else ""
        return (
            f"{prefix}: {command}\n"
            f"Exit code: {completed.returncode}\n"
            f"{notes_text}"
            f"--- stdout ---\n{stdout}\n"
            f"--- stderr ---\n{stderr}"
        )

    # 流式模式：Popen + 读线程
    if on_command_start:
        on_command_start(command)

    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as exc:
        if on_command_end:
            on_command_end(-1, "", str(exc))
        return f"[execute_command] Failed to run `{command}`: {exc}"

    stdout_parts: List[str] = []
    stderr_parts: List[str] = []

    t_stdout = threading.Thread(
        target=_read_stream,
        args=(process.stdout, "stdout", stdout_parts, on_command_output),
    )
    t_stderr = threading.Thread(
        target=_read_stream,
        args=(process.stderr, "stderr", stderr_parts, on_command_output),
    )
    t_stdout.daemon = True
    t_stderr.daemon = True
    t_stdout.start()
    t_stderr.start()

    try:
        returncode = process.wait(timeout=timeout_float)
    except subprocess.TimeoutExpired:
        # 仅对非长时间运行命令应用超时终止逻辑
        process.kill()
        process.wait()
        returncode = -1
        timeout_msg = (
            f"Timeout after {timeout_float} seconds while running: {command}"
        )
        if on_command_end:
            on_command_end(-1, "\n".join(stdout_parts), timeout_msg)
        return f"[execute_command] {timeout_msg}"
    finally:
        if process.stdout:
            try:
                process.stdout.close()
            except OSError:
                pass
        if process.stderr:
            try:
                process.stderr.close()
            except OSError:
                pass
        t_stdout.join(timeout=2.0)
        t_stderr.join(timeout=2.0)

    full_stdout = "".join(stdout_parts)
    full_stderr = "".join(stderr_parts)
    if on_command_end:
        on_command_end(returncode, full_stdout, full_stderr)

    prefix = (
        "[execute_command] Long-running command"
        if long_running
        else "[execute_command] Command"
    )
    notes: List[str] = []
    if returncode == -1:
        notes.append(
            "Exit code -1 usually means the process was killed by a timeout or external signal."
        )
    if long_running:
        notes.append(
            "This command was marked as long-running; it is expected to keep running until it is stopped externally."
        )
    notes_text = ("\n".join(notes) + "\n") if notes else ""

    return (
        f"{prefix}: {command}\n"
        f"Exit code: {returncode}\n"
        f"{notes_text}"
        f"--- stdout ---\n{full_stdout}\n"
        f"--- stderr ---\n{full_stderr}"
    )


def write_file_tool(args: Mapping[str, Any]) -> str:
    # 要写入的目标相对路径；不能为空
    rel_path = str(args.get("path") or "").strip()
    if not rel_path:
        raise ValueError("`path` is required")

    # 写入内容可以为空字符串（例如清空文件）
    content = str(args.get("content") or "")
    # 使用 _ensure_within_workspace 做路径安全校验（基于当前任务 workspace_root）
    target = _ensure_within_workspace(get_effective_workspace_root() / rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"[write_to_file] Wrote {target}"


def get_available_port_tool(args: Mapping[str, Any]) -> str:
    """Find a free TCP port, optionally from minPort upward. The port is only probed, not held."""
    min_port_raw = args.get("minPort")
    if min_port_raw is not None:
        try:
            min_port = int(min_port_raw)
        except (TypeError, ValueError):
            min_port = 8080
    else:
        min_port = 8080
    min_port = max(1, min(min_port, 65535))

    for port in range(min_port, 65536):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", port))
            sock.close()
            return f"[get_available_port] Available port: {port}"
        except OSError:
            continue
    raise ValueError(f"[get_available_port] No free port found in range {min_port}-65535")


def run_tool_call(
    tool_call: Any,
    *,
    execute_command_hooks: Optional[
        tuple[
            Optional[Callable[[str], None]],
            Optional[Callable[[str, Literal["stdout", "stderr"]], None]],
            Optional[Callable[[int, str, str], None]],
        ]
    ] = None,
) -> str:
    """
    将 LLM 返回的 tool_call 映射到具体工具实现。

    兼容 OpenAI Python SDK 的对象形式和普通 dict 形式。
    execute_command_hooks 可选，为 (on_command_start, on_command_output, on_command_end)。
    """

    # 小工具：同时兼容 SDK 对象属性访问和 dict 访问
    def _get(obj: Any, name: str, default: Any = None) -> Any:
        if hasattr(obj, name):
            return getattr(obj, name)
        if isinstance(obj, MutableMapping):
            return obj.get(name, default)
        return default

    # 从 tool_call 中提取函数名与 JSON 参数
    function = _get(tool_call, "function")
    name = _get(function, "name")
    arguments_json = _get(function, "arguments", "{}") or "{}"

    try:
        arguments = json.loads(arguments_json)
    except Exception as exc:
        raise ValueError(f"Failed to parse tool arguments JSON for {name}: {exc}") from exc

    # 根据工具名分发到具体实现
    if name == "execute_command":
        kwargs: Dict[str, Any] = {}
        if execute_command_hooks:
            kwargs["on_command_start"] = execute_command_hooks[0]
            kwargs["on_command_output"] = execute_command_hooks[1]
            kwargs["on_command_end"] = execute_command_hooks[2]
        return execute_command_tool(arguments, **kwargs)
    if name == "write_to_file":
        return write_file_tool(arguments)
    if name == "get_available_port":
        return get_available_port_tool(arguments)

    raise ValueError(f"Unknown tool name: {name}")

