from __future__ import annotations

import json
import socket
import subprocess
import threading
import concurrent.futures
import logging
import time
import re
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

_auto_stdin_logger = logging.getLogger("agent.auto_stdin")


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
    on_output: Optional[Callable[[str, Literal["stdout", "stderr", "stdin"]], None]],
    on_raw_char: Optional[Callable[[str, Literal["stdout", "stderr"]], None]] = None,
) -> None:
    """在单独线程中读取管道。

    - 由于 Expo CLI / 其它交互式程序的提示可能不以换行结尾（会导致 readline() 阻塞），
      这里改为按字符读取，并在检测到 '\n' 后再按行回调。
    - 同时把“原始字符流”交给上层做 prompt detector。
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
            if on_raw_char:
                on_raw_char(ch, stream_name)
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
    on_command_output: Optional[Callable[[str, Literal["stdout", "stderr", "stdin"]], None]] = None,
    on_command_end: Optional[Callable[[int, str, str], None]] = None,
    on_command_input_request: Optional[Callable[[str], str]] = None,
    max_auto_inputs: int = 5,
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
        or on_command_input_request is not None
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
        stdin = subprocess.PIPE if on_command_input_request is not None else subprocess.DEVNULL
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
            stdin=stdin,
        )
    except Exception as exc:
        if on_command_end:
            on_command_end(-1, "", str(exc))
        return f"[execute_command] Failed to run `{command}`: {exc}"

    stdout_parts: List[str] = []
    stderr_parts: List[str] = []

    # ------- 自动交互输入（AI 回应交互式 stdin） -------
    # 注意：_read_stream 会在读线程里触发 on_raw_char；因此这里需要用锁保证：
    # - 同一时间只允许一次自动输入决策（避免重复写 stdin）
    # - LLM 调用期间不会并发触发多个输入
    auto_input_lock = threading.Lock()
    auto_input_count = 0
    auto_input_timeout_seconds = 25.0
    auto_input_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    raw_prompt_buffer = ""
    raw_buffer_max_chars = 4000
    last_prompt_scan_log_ts = 0.0

    def _detect_prompt(buf: str) -> Optional[dict[str, Any]]:
        # 返回结构示例：
        # { "kind": "yesno"|"presskey"|"unknown", "prompt": "...", "keys": [...] }
        # 归一化输入，兼容：
        # - 终端/CLI 可能插入 ANSI escape code
        # - 全角问号等字符差异
        # - prompt 末尾可能出现 `»` / 其它装饰字符
        cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buf)
        cleaned = cleaned.replace("？", "?")
        lowered = cleaned.lower()

        # 1) Port conflict / yes/no 类：识别 Use port <n> instead + (Y/n) / (y/N) 之类
        # 形如：`? Use port 8083 instead? » (Y/n)`
        port_m = re.search(
            r"Use\s+port\s+(\d+)\s+instead",
            cleaned,
            flags=re.IGNORECASE,
        )
        yesno_m = re.search(r"\(([Yy])\s*/\s*([Nn])\)", cleaned)
        if port_m and yesno_m:
            tail = cleaned[-600:]
            # Determine default from the case of the letters in the prompt:
            # - (Y/n) => default "Y"
            # - (y/N) => default "N"
            # (Y/n) => default "y"; (y/N) => default "n"
            default_choice = "y"
            if yesno_m.group(1) == "y" and yesno_m.group(2) == "N":
                default_choice = "n"
            return {
                "kind": "yesno",
                "prompt": (
                    "[AUTO_PROMPT]\nPort conflict detected.\n"
                    f"PROMPT_TEXT:\n{tail}"
                ),
                "keys": [port_m.group(1)],
                "default": default_choice,
            }

        # 2) 通用 yes/no：识别 (Y/n) / (y/N) / (y/n) 等
        yesno_general_m = re.search(r"\(([Yy])\s*/\s*([Nn])\)", cleaned)
        if yesno_general_m:
            tail = cleaned[-600:]
            # (Y/n) => default "y"; (y/N) => default "n"
            default_choice = "y"
            if yesno_general_m.group(1) == "y" and yesno_general_m.group(2) == "N":
                default_choice = "n"
            return {
                "kind": "yesno",
                "prompt": (
                    "[AUTO_PROMPT]\nYes/No confirmation detected.\n"
                    f"PROMPT_TEXT:\n{tail}"
                ),
                "keys": [],
                "default": default_choice,
            }

        # 3) Press <key> 类：expo-cli 常见 `Press s │ switch...`
        # 这里不强依赖 `│`，只要看到 `Press <alnum>` 就提取 key 候选。
        if "press" in lowered:
            keys = re.findall(r"Press\s+([a-zA-Z0-9])", cleaned)
            if keys:
                # 去重但保持顺序
                seen: set[str] = set()
                uniq_keys: list[str] = []
                for k in keys:
                    if k not in seen:
                        seen.add(k)
                        uniq_keys.append(k)
                tail = cleaned[-900:]
                return {
                    "kind": "presskey",
                    "prompt": (
                        "[AUTO_PROMPT]\nExpo keypress detected.\n"
                        f"CANDIDATE_KEYS:{uniq_keys}\n"
                        f"PROMPT_TEXT:\n{tail}"
                    ),
                    "keys": uniq_keys,
                }

        return None

    def _sanitize_input_for_kind(kind: str, raw_text: str, candidate_keys: list[str]) -> str:
        txt = (raw_text or "").strip()
        if not txt:
            # 默认兜底：是 yes/no 就回答 y；是 keypress 就选第一个 candidate
            if kind == "yesno":
                return "y"
            if kind == "presskey" and candidate_keys:
                return candidate_keys[0]
            return ""

        low = txt.lower()
        if kind == "yesno":
            if low in {"y", "yes"}:
                return "y"
            if low in {"n", "no"}:
                return "n"
            # 若模型输出了其它内容，优先猜第一个 y/n
            if "y" in low and "n" not in low:
                return "y"
            if "n" in low and "y" not in low:
                return "n"
            return "y"

        if kind == "presskey":
            # 只取第一个候选字符；优先落在 candidate keys 里
            for ch in txt:
                if not ch.strip():
                    continue
                if not candidate_keys or ch in candidate_keys:
                    return ch
            return candidate_keys[0] if candidate_keys else ""

        # unknown：尽量原样但去掉尾随换行
        return txt.replace("\r", "").replace("\n", "")

    t_stdout = threading.Thread(
        target=_read_stream,
        args=(process.stdout, "stdout", stdout_parts, on_command_output, None),
    )
    t_stderr = threading.Thread(
        target=_read_stream,
        args=(process.stderr, "stderr", stderr_parts, on_command_output, None),
    )

    def _on_raw_char(ch: str, stream: Literal["stdout", "stderr"]) -> None:
        # stream 参数暂时未用于差异化处理，但预留给后续扩展（例如只从 stdout 探测）
        nonlocal raw_prompt_buffer, auto_input_count
        # 仅处理当 stdin 可写时（即存在 on_command_input_request）
        if on_command_input_request is None:
            return

        raw_prompt_buffer += ch
        if len(raw_prompt_buffer) > raw_buffer_max_chars:
            raw_prompt_buffer = raw_prompt_buffer[-raw_buffer_max_chars:]

        # 增强可观测性：如果缓冲区里出现了 Expo 典型交互关键字，做一次节流日志（避免每字符刷屏）。
        try:
            lowered_buf = raw_prompt_buffer.lower()
            if ("use port" in lowered_buf) or ("press " in lowered_buf):
                import time as _time

                now_ts = _time.time()
                nonlocal last_prompt_scan_log_ts
                if now_ts - last_prompt_scan_log_ts > 1.0:
                    last_prompt_scan_log_ts = now_ts
                    _auto_stdin_logger.info(
                        "AUTO_PROMPT_SCAN tail=%r",
                        raw_prompt_buffer[-200:],
                    )
        except Exception:
            pass

        detected = _detect_prompt(raw_prompt_buffer)
        if not detected:
            return

        # 自动输入有上限；避免无限循环
        if auto_input_count >= max_auto_inputs:
            return

        # 防止并发重复输入
        if not auto_input_lock.acquire(blocking=False):
            return

        # 抢占成功后立即清空缓冲区，避免同一 prompt 在等待 LLM 时重复触发
        try:
            auto_input_count += 1
            kind = detected.get("kind") or "unknown"
            candidate_keys = detected.get("keys") or []
            default_choice = detected.get("default")
            prompt_text = detected.get("prompt") or raw_prompt_buffer

            raw_prompt_buffer = ""

            # --- deterministic stdin decision for known prompt kinds ---
            # 为了避免 LLM/超时导致的不确定性：当我们已能明确识别 yes/no 或 press key 时，
            # 直接写入确定性输入；仅 unknown 或缺信息时才调用 LLM。
            deterministic_input: Optional[str] = None
            if kind == "yesno":
                # Prefer detected default if available; otherwise default to "y".
                deterministic_input = default_choice or "y"
            elif kind == "presskey":
                # Prefer 'w' (open web) if present; otherwise pick the first candidate.
                if candidate_keys:
                    deterministic_input = "w" if "w" in candidate_keys else candidate_keys[0]
                else:
                    deterministic_input = None

            if deterministic_input is not None:
                model_input = deterministic_input
            else:
                # fallback to LLM only when we can't decide deterministically
                try:
                    future = auto_input_executor.submit(
                        on_command_input_request, prompt_text
                    )
                    model_input = future.result(timeout=auto_input_timeout_seconds)
                except Exception as exc:
                    # LLM 失败时降级为兜底策略
                    if on_command_output:
                        try:
                            on_command_output(f"[auto-input-error] {exc}\n", "stdin")
                        except Exception:
                            pass
                    model_input = ""

            sanitized = _sanitize_input_for_kind(kind, model_input, candidate_keys)
            if not sanitized:
                return

            if process.stdin is None:
                return

            # yes/no 类通常需要换行结束输入；keypress 类尽量只写单字符，避免额外换行造成“下一次输入”
            if kind == "presskey":
                process.stdin.write(sanitized)
            else:
                process.stdin.write(sanitized + "\n")
            process.stdin.flush()

            # 可观测性：写入前先打点，方便你确认 detector 命中 + stdin 写入是否发生。
            try:
                _auto_stdin_logger.info(
                    "AUTO_STDIN_WRITE kind=%s default=%r candidates=%r raw_input=%r written=%r",
                    kind,
                    default_choice,
                    candidate_keys,
                    model_input,
                    sanitized,
                )
            except Exception:
                pass

            if on_command_output:
                try:
                    on_command_output(f"[auto-stdin] {sanitized}\n", "stdin")
                except Exception:
                    pass
        finally:
            auto_input_lock.release()

    # 重新创建线程（需要把 on_raw_char 绑定进 args）
    t_stdout = threading.Thread(
        target=_read_stream,
        args=(process.stdout, "stdout", stdout_parts, on_command_output, _on_raw_char),
    )
    t_stderr = threading.Thread(
        target=_read_stream,
        args=(process.stderr, "stderr", stderr_parts, on_command_output, _on_raw_char),
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
        try:
            auto_input_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
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
        # 注意：这属于“探测时”的严格约束；我们会在探测结束后立刻 close socket。
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
            Optional[Callable[[str], str]],
        ]
    ] = None,
) -> str:
    """
    将 LLM 返回的 tool_call 映射到具体工具实现。

    兼容 OpenAI Python SDK 的对象形式和普通 dict 形式。
    execute_command_hooks 可选，为：
      (on_command_start, on_command_output, on_command_end, on_command_input_request)
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
            kwargs["on_command_input_request"] = execute_command_hooks[3]
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

