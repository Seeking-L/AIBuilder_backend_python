from __future__ import annotations

import shutil
import time
from pathlib import Path

from storage.sqlite_store import SqliteStore


def cleanup_expired_conversations(
    *,
    db_path: Path,
    workspace_root: Path,
    ttl_days: int,
) -> int:
    """清理过期 conversation：

    - 删除 DB 中 conversation / runs / messages / agent_events 等数据
    - 删除 generated/<conversation-id>/ 对应工程目录（包含 baseExpo 及其修改文件）
    """

    store = SqliteStore(db_path)

    ttl_seconds = max(0, int(ttl_days)) * 86400
    cutoff_ts = int(time.time()) - ttl_seconds

    expired = store.list_expired_conversations(cutoff_ts=cutoff_ts)
    for conv in expired:
        conversation_id = conv["conversation_id"]
        conversation_workspace_root = conv["workspace_root"]

        # 1) 删除 DB 数据
        store.delete_conversation_data(conversation_id=conversation_id)

        # 2) 删除 workspace 目录（容忍目录已不存在）
        try:
            shutil.rmtree(conversation_workspace_root, ignore_errors=True)
        except Exception:
            # 不阻塞整体清理
            pass

    return len(expired)

