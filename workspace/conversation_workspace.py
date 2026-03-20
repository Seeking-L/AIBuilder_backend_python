from __future__ import annotations

import shutil
from pathlib import Path
from typing import Tuple

from config import settings


def prepare_conversation_workspace(conversation_id: str) -> Tuple[str, str]:
    """为一个 conversation_id 初始化并返回 workspace_root / expo_root。

    设计目标：
    - 每个窗口（conversation）对应一套持久化的 Expo 工程目录。
    - 仅在创建窗口时拷贝一次 BaseCodeForAI/baseExpo 模板到 generated/<conversation-id>/baseExpo。
    - 后续多轮 run 复用同一份 expo_root，不允许 rmtree 清空（满足“刷新后仍保留工程代码”）。
    """

    backend_root = Path(__file__).resolve().parent.parent
    template_expo_root = backend_root / "BaseCodeForAI" / "baseExpo"
    if not template_expo_root.exists():
        raise FileNotFoundError(f"Template expo root not found: {template_expo_root}")

    conversation_workspace_root = settings.workspace_root / "generated" / conversation_id
    expo_root = conversation_workspace_root / "baseExpo"

    conversation_workspace_root.mkdir(parents=True, exist_ok=True)

    # 只在 expo_root 不存在时拷贝模板；不做 rmtree，避免清空已修改的工程
    if not expo_root.exists():
        shutil.copytree(
            template_expo_root,
            expo_root,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("node_modules"),
        )

    return str(conversation_workspace_root), str(expo_root)

