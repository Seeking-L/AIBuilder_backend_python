from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

# 默认从当前目录及上级目录查找 .env，
# 这样可以复用现有 Node 端的 .env（位于项目根目录或上级）。
# 若两处都不存在，则退回到 python-dotenv 的默认搜索逻辑。
for candidate in (BASE_DIR / ".env", BASE_DIR.parent / ".env"):
    if candidate.is_file():
        load_dotenv(dotenv_path=candidate)
        break
else:
    # 兜底：也尝试加载默认搜索路径下的 .env
    load_dotenv()


def _require_env(name: str, fallback: str | None = None) -> str:
    """获取必填环境变量，若无则用 fallback；若两者都无则抛错。

    用法举例：
    - _require_env("MODEL_NAME", "gpt-4.1")
    - _require_env("SOME_REQUIRED_KEY")  # 若未设置会立即中断启动
    """
    value = os.getenv(name, fallback)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    port: int
    workspace_root: Path
    # agent.progress 日志文件路径（绝对路径）
    progress_log_path: Path
    model_provider: str
    model_name: str
    openai_api_key: str | None
    kimi_api_key: str | None
    anthropic_api_key: str | None
    # 多轮任务循环的最大轮数（如 agent.task_loop 中的最大对话轮次）
    max_task_rounds: int
    # 千问 / DashScope API key（用于 MODEL_PROVIDER=qwen）
    qwen_api_key: str | None
    # DashScope OpenAI 兼容接口 base_url（可通过环境变量覆盖不同区域）
    dashscope_base_url: str


def _resolve_workspace_root() -> Path:
    """工作区根目录：优先 WORKSPACE_ROOT，否则为项目上级的 AIBuilder_workspace。

    这样设计的目的是：
    - 允许通过环境变量显式指定一个工作区目录，方便部署到不同环境；
    - 若未指定，则在当前项目上级创建/使用一个固定命名的目录，便于前端和后端约定共享。
    """
    workspace_root_from_env = os.getenv("WORKSPACE_ROOT")
    if workspace_root_from_env:
        return Path(workspace_root_from_env).expanduser().resolve()
    return (BASE_DIR.parent / "AIBuilder_workspace").resolve()


def _resolve_progress_log_path() -> Path:
    """进度日志文件路径：优先 PROGRESS_LOG_PATH，否则为项目根目录 logs/agent-progress.log。"""
    log_path_env = os.getenv("PROGRESS_LOG_PATH")
    base_root = BASE_DIR.parent
    if log_path_env:
        p = Path(log_path_env).expanduser()
        if not p.is_absolute():
            p = (base_root / p).resolve()
        return p
    return (base_root / "logs" / "agent-progress.log").resolve()


settings = Settings(
    port=int(os.getenv("PORT", "4000")),
    workspace_root=_resolve_workspace_root(),
    progress_log_path=_resolve_progress_log_path(),
    model_provider=os.getenv("MODEL_PROVIDER", "openai"),
    model_name=_require_env("MODEL_NAME", "gpt-4.1"),
    openai_api_key=os.getenv("OPENAI_API_KEY"),
    kimi_api_key=os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY"),
    anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
    max_task_rounds=int(os.getenv("MAX_TASK_ROUNDS", "5")),
    qwen_api_key=os.getenv("DASHSCOPE_API_KEY"),
    dashscope_base_url=os.getenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
)

