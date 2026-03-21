from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
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
            "description": (
                "Execute a shell command in the project workspace (for tests, builds, dev servers, etc). "
                "Commands always run in a non-interactive child environment: stdin is closed (no prompts), "
                "and CI=1 / EXPO_NO_INTERACTIVE=1 are set so tools like Expo CLI should not block on y/n. "
                "Do not rely on interactive prompts; pass explicit flags (e.g. --port after get_available_port)."
            ),
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
                        "description": (
                            "Set true for long-running dev servers (e.g. Metro). Disables normal command timeout; "
                            "the tool returns automatically once the server prints a ready line (e.g. Waiting on http://localhost:...) "
                            "while the child process keeps running in the background."
                        ),
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
            "description": "Get a single TCP port that is currently free on this machine (strict TCP bind probe on IPv4+IPv6). Only use when you have an explicit need to pass a port to a dev server command.",
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
    {
        "type": "function",
        "function": {
            "name": "notify_expo_url_ready",
            # 给“模型->后端->前端”贯通增加一个轻量通知 tool：
            # - 由模型在拿到可用的 `exp://...` 后调用
            # - 后端负责把 URL 转成前端可用的事件，并驱动“查看应用”按钮出现
            # - 本 tool 的返回值只用于 LLM 上下文（tool_result），真正的转发逻辑在 `agent/task_loop.py`
            "description": "Notify the backend that an Expo Go URL (exp://...) is ready for the frontend to open. Provide expoUrl for the task run.",
            "parameters": {
                "type": "object",
                "properties": {
                    # prefer model to pass this explicitly, so backend doesn't rely only on log parsing
                    "expoUrl": {
                        "type": "string",
                        "description": "Expo Go URL like exp://<ip>:<port>. Must be reachable by the user's phone on LAN.",
                    },
                },
                # expoUrl 尽量由模型提供；但为了兼容异常情况，本 tool 的 schema 不强制必填，
                # 后端会在 task_loop 里做兜底提取/校验。
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]

# 为兼容旧代码，提供小写别名。
# 其它模块可以继续从 `tool_definitions` 导入，而不关心常量名是否变化。
tool_definitions: List[ToolDefinition] = TOOL_DEFINITIONS


# Metro / Expo 已就绪时常见的日志片段（与 `task_loop` 里 `register_dev_server_running` 的判定保持一致）。
_LONG_RUNNING_READY_MARKERS: tuple[str, ...] = (
    "Waiting on http://localhost:",
)
# longRunning 时等待上述就绪日志的最长时间（秒）；超时则杀进程并返回错误。
_LONG_RUNNING_STARTUP_TIMEOUT_SEC = 180.0
# 传给 `on_command_end` 的约定：子进程仍在跑，工具为释放 agent 循环而提前返回。
EXIT_CODE_TOOL_DETACHED: int = -2


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


def _command_child_env() -> dict[str, str]:
    """为子进程准备环境变量：强制“非交互”语义，避免 Expo/npm 等卡在 y/n 提示上。

    - ``CI=1``：广泛被 CI 工具链识别；Expo CLI 会据此减少或禁用交互式 prompt。
    - ``EXPO_NO_INTERACTIVE=1``：对 Expo 相关 CLI 的额外提示，与 ``CI`` 叠加更稳。

    说明：对 ``os.environ`` 做浅拷贝再覆盖键，避免修改当前进程全局环境。
    """
    env: dict[str, str] = dict(os.environ)
    env["CI"] = "1"
    env["EXPO_NO_INTERACTIVE"] = "1"
    return env


def _read_stream(
    pipe: Any,
    stream_name: Literal["stdout", "stderr"],
    parts: List[str],
    on_output: Optional[Callable[[str, Literal["stdout", "stderr"]], None]],
) -> None:
    """在单独线程中读取管道。

    按字符读取并在遇到换行符后整行回调，避免某些 CLI 长时间不输出 ``\\n`` 时
    `readline()` 一直阻塞、上层看不到半行提示的情况。
    """
    if pipe is None:
        return
    try:
        line_buf = ""
        while True:
            ch = pipe.read(1)
            if ch == "":
                break
            line_buf += ch
            if ch == "\n":
                parts.append(line_buf)
                if on_output:
                    on_output(line_buf, stream_name)
                line_buf = ""
        # 进程退出后可能残留半行（没有 '\n'）
        if line_buf:
            parts.append(line_buf)
            if on_output:
                on_output(line_buf, stream_name)
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

    child_env = _command_child_env()

    use_streaming = (
        on_command_start is not None
        or on_command_output is not None
        or on_command_end is not None
    )

    if not use_streaming:
        # longRunning 必须走流式路径，否则 subprocess.run(..., timeout=None) 会永远等子进程退出。
        if long_running:
            return (
                "[execute_command] longRunning=true requires streaming command hooks "
                "(internal error: the agent loop must pass output hooks for dev servers)."
            )
        # 无回调：一次性收集输出；stdin 关闭 + 非交互环境，避免子进程等待用户输入
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
                stdin=subprocess.DEVNULL,
                env=child_env,
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

    # 流式模式：Popen + 读线程（仍不打开 stdin，避免交互阻塞）
    if on_command_start:
        on_command_start(command)

    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
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

    detached_early = False
    returncode = 0

    try:
        if long_running:
            # 长驻进程不能 process.wait(None)，否则工具永不返回、对话卡住。
            # 在输出中出现 Metro 就绪行后提前返回；子进程与读线程继续排空管道，避免 Metro 写满缓冲区阻塞。
            deadline = time.monotonic() + _LONG_RUNNING_STARTUP_TIMEOUT_SEC
            while True:
                combined = "".join(stdout_parts) + "".join(stderr_parts)
                if any(marker in combined for marker in _LONG_RUNNING_READY_MARKERS):
                    full_stdout = "".join(stdout_parts)
                    full_stderr = "".join(stderr_parts)
                    detached_early = True
                    if on_command_end:
                        on_command_end(
                            EXIT_CODE_TOOL_DETACHED, full_stdout, full_stderr
                        )
                    prefix = "[execute_command] Long-running command"
                    notes = [
                        "Metro / dev server reported it is listening; tool returned so the agent can continue.",
                        "The child process is still running; readers keep draining stdout/stderr in the background.",
                    ]
                    notes_text = ("\n".join(notes) + "\n") if notes else ""
                    return (
                        f"{prefix}: {command}\n"
                        f"Exit code: 0 (dev server ready; tool returned early while process keeps running)\n"
                        f"{notes_text}"
                        f"--- stdout ---\n{full_stdout}\n"
                        f"--- stderr ---\n{full_stderr}"
                    )
                rc = process.poll()
                if rc is not None:
                    returncode = rc
                    break
                if time.monotonic() > deadline:
                    process.kill()
                    try:
                        process.wait(timeout=30)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    returncode = -1
                    break
                time.sleep(0.15)

            if process.poll() is None:
                try:
                    returncode = process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    returncode = -1
        else:
            try:
                returncode = process.wait(timeout=timeout_float)
            except subprocess.TimeoutExpired:
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
        if not detached_early:
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
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
            "This command was marked as long-running; the child exited before a ready line was detected "
            f"(waited up to {_LONG_RUNNING_STARTUP_TIMEOUT_SEC:.0f}s), or exited normally."
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
    """Find a free TCP port, optionally from minPort upward.

    注意：由于工具与 dev server 启动之间存在时间差，任何“选端口”都有理论竞态；
    本实现通过严格的 IPv4/IPv6 bind 探测来减少“假阳性”（探测到空闲但实际上会冲突）。
    """
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
            if _is_tcp_port_free_probe(port):
                return f"[get_available_port] Available port: {port}"
        except Exception:
            # 探测失败继续下一个端口，避免工具因单端口异常中断整个搜索
            continue
    raise ValueError(
        f"[get_available_port] No free port found in range {min_port}-65535"
    )


# -----------------------------
# TCP 端口探测（严格探测）
# -----------------------------
def _close_sockets(sockets: List[socket.socket]) -> None:
    for s in sockets:
        try:
            s.close()
        except OSError:
            pass


def _is_tcp_port_free_probe(port: int) -> bool:
    """尝试在 IPv4 + IPv6 上都绑定并进入 listen 状态（立刻释放）。

    该函数的目标是尽可能“减少假阳性”，而不是跨进程/跨时间保持占用。
    """
    sockets: List[socket.socket] = []

    # Windows 上更准确的专用绑定选项（若平台不支持会返回 None）
    exclusive_opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)

    def _create_and_bind(af: socket.AddressFamily, bind_addr: object) -> socket.socket:
        sock = socket.socket(af, socket.SOCK_STREAM)

        # SO_EXCLUSIVEADDRUSE 能更严格地防止其它进程占用同一地址/端口。
        # 注意：这属于“探测时”的约束；我们会在探测结束后立刻 close socket。
        if exclusive_opt is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, exclusive_opt, 1)  # type: ignore[arg-type]
            except OSError:
                # 若设置失败，仍继续进行 bind 尝试；由 bind 的结果决定是否可用
                pass

        # 同时开始 listen，比单纯 bind 更接近“实际服务启动时”的端口状态。
        # bind/listen 只作为探测用途：探测成功后立刻 close。
        sock.bind(bind_addr)  # may raise OSError
        sock.listen(1)
        return sock

    try:
        # IPv4：通常 Expo/Node 服务最终会落到 IPv4 bind（例如 localhost/0.0.0.0）
        sockets.append(_create_and_bind(socket.AF_INET, ("0.0.0.0", port)))

        # IPv6：严格一些，避免返回“实际会冲突”的端口
        sockets.append(_create_and_bind(socket.AF_INET6, ("::", port)))
    except OSError:
        return False
    finally:
        # 探测结束立刻释放，避免影响 dev server 在后续步骤里绑定端口。
        _close_sockets(sockets)

    return True


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
    execute_command_hooks 可选，为：
      (on_command_start, on_command_output, on_command_end)
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
    if name == "notify_expo_url_ready":
        # 该 tool 本身不执行任何外部操作，仅作为模型“通知意图”的载体。
        # 真正把 expoUrl 变成前端可见事件的逻辑，放在 `agent/task_loop.py` 的 tool_calls 循环里完成。
        #
        # 返回值用于作为 tool_result 回填给模型，便于模型在后续步骤中理解“通知已被接收”。
        return "[notify_expo_url_ready] acknowledged"

    raise ValueError(f"Unknown tool name: {name}")
