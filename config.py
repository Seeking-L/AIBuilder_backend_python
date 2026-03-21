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
    # SQLite 用于持久化 conversation/windows/messages/events
    db_path: Path
    # 对话摘要触发阈值（当历史过长时压缩到 summary memory）
    summary_trigger_chars: int
    summary_trigger_messages: int
    # 构造给模型的“最近 N 条消息”
    recent_messages_for_model: int
    # CORS 允许的前端源（当需要 cookie 时，不能使用 allow_origins=["*"]）
    cors_allow_origins: list[str]

    # 当模型传入 exp://localhost:... 时，回填为可供手机 Expo Go 访问的主机（显式 IP/域名）。
    # 不设则运行时自动探测本机局域网 IPv4；Docker/多网卡环境建议显式配置。
    expo_lan_host: str | None

    # conversation 过期清理策略（按“最近一次 updated_at”计算）
    conversation_ttl_days: int
    cleanup_on_startup: bool


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


def _resolve_db_path(workspace_root: Path) -> Path:
    """SQLite DB 路径：默认放到工作区根目录下。"""
    return (workspace_root / "aibuilder.sqlite3").resolve()


def _resolve_cors_allow_origins() -> list[str]:
    """解析 CORS_ALLOW_ORIGINS：逗号分隔的允许源列表。"""

    raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]

    # 默认兜底：常见本地开发地址；生产环境建议显式配置 CORS_ALLOW_ORIGINS
    return [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4000",
        "http://127.0.0.1:4000",
    ]


def _optional_nonempty_str(value: str | None) -> str | None:
    """环境变量常见空串视为未设置。"""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


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
    db_path=_resolve_db_path(_resolve_workspace_root()),
    summary_trigger_chars=int(os.getenv("SUMMARY_TRIGGER_CHARS", "12000")),
    summary_trigger_messages=int(os.getenv("SUMMARY_TRIGGER_MESSAGES", "40")),
    recent_messages_for_model=int(os.getenv("RECENT_MESSAGES_FOR_MODEL", "30")),
    cors_allow_origins=_resolve_cors_allow_origins(),
    expo_lan_host=_optional_nonempty_str(os.getenv("EXPO_LAN_HOST")),
    conversation_ttl_days=int(os.getenv("CONVERSATION_TTL_DAYS", "7")),
    cleanup_on_startup=os.getenv("CLEANUP_ON_STARTUP", "false").strip().lower()
    in {"1", "true", "yes", "y", "on"},
)

