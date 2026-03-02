from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping

from config import settings


ToolDefinition = Dict[str, Any]


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
]

# 为兼容旧代码，提供小写别名。
# 其它模块可以继续从 `tool_definitions` 导入，而不关心常量名是否变化。
tool_definitions: List[ToolDefinition] = TOOL_DEFINITIONS


def _ensure_within_workspace(path: Path) -> Path:
    # 确保目标路径在 workspace_root 内部，防止通过相对路径“逃逸”到不安全的位置。
    base = settings.workspace_root.resolve()
    target = path.resolve()
    if not str(target).startswith(str(base)):
        raise ValueError("Target path escapes workspace root")
    return target


def execute_command_tool(args: Mapping[str, Any]) -> str:
    # 从 tool 调用参数中解析出要执行的命令；若缺失则报错
    command = str(args.get("command") or "").strip()
    if not command:
        raise ValueError("`command` is required")

    # 可选工作目录（相对路径），以及可选超时时间
    cwd_arg = args.get("cwd")
    timeout_seconds = args.get("timeoutSeconds")

    # 所有命令都必须在 workspace_root 内执行
    base = settings.workspace_root.resolve()
    if cwd_arg:
        cwd_path = _ensure_within_workspace(base / str(cwd_arg))
    else:
        cwd_path = base

    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=float(timeout_seconds) if timeout_seconds is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        return f"[execute_command] Timeout after {exc.timeout} seconds while running: {command}"
    except Exception as exc:
        return f"[execute_command] Failed to run `{command}`: {exc}"

    # subprocess.run 完成后，分别提取标准输出和错误输出
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    return (
        f"[execute_command] Command: {command}\n"
        f"Exit code: {completed.returncode}\n"
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )


def write_file_tool(args: Mapping[str, Any]) -> str:
    # 要写入的目标相对路径；不能为空
    rel_path = str(args.get("path") or "").strip()
    if not rel_path:
        raise ValueError("`path` is required")

    # 写入内容可以为空字符串（例如清空文件）
    content = str(args.get("content") or "")
    base = settings.workspace_root.resolve()
    # 使用 _ensure_within_workspace 做额外的路径安全校验
    target = _ensure_within_workspace(base / rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"[write_to_file] Wrote {target}"


def run_tool_call(tool_call: Any) -> str:
    """
    将 LLM 返回的 tool_call 映射到具体工具实现。

    兼容 OpenAI Python SDK 的对象形式和普通 dict 形式。
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
        return execute_command_tool(arguments)
    if name == "write_to_file":
        return write_file_tool(arguments)

    raise ValueError(f"Unknown tool name: {name}")

