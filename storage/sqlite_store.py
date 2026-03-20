from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
import shutil
import threading
from typing import Any, Iterable, Optional
from uuid import uuid4


@dataclass(frozen=True)
class RunStatus:
    pending: str = "pending"
    running: str = "running"
    completed: str = "completed"
    failed: str = "failed"


class SqliteStore:
    """SQLite 持久化层（用于窗口列表/消息历史/AgentEvent 时间线）。

    设计要点：
    - 仅使用 stdlib sqlite3，无额外依赖。
    - 通过 WAL 模式支持“写入 run 事件 / 读取 WS 增量事件”的并发访问。
    - 提供尽量细粒度的读写方法，方便后续把 agent 事件逐步落库。
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite 事务是“连接级别”的：当多个线程共享同一个 connection 时，
        # 容易出现 `cannot start a transaction within a transaction` 之类错误。
        # 使用统一写锁把所有会写入/commit 的操作串行化即可避免该问题。
        self._write_lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        # WAL 可以改善“一个进程内多线程读写”的体感
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")

        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                title TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                workspace_root TEXT NOT NULL,
                expo_root TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id)
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                input_text TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                finished_at INTEGER,
                error TEXT,
                final_text TEXT,
                expo_url TEXT,
                FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
            );

            -- message_seq 用自增主键保证可按序读取
            CREATE TABLE IF NOT EXISTS messages (
                message_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_call_id TEXT,
                tool_calls_json TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
            );

            CREATE TABLE IF NOT EXISTS agent_events (
                event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                step_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                summary_text TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_session_updated ON conversations(session_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_seq ON messages(conversation_id, message_seq ASC);
            CREATE INDEX IF NOT EXISTS idx_agent_events_run_step ON agent_events(run_id, step_id ASC, event_seq ASC);
            CREATE INDEX IF NOT EXISTS idx_runs_conversation_created ON runs(conversation_id, created_at DESC);
            """
        )
        self._conn.commit()

    @property
    def status(self) -> RunStatus:
        return RunStatus()

    def _now(self) -> int:
        return int(time.time())

    # ----------------------------- session -----------------------------

    def touch_session(self, session_id: str) -> None:
        with self._write_lock:
            now = self._now()
            self._conn.execute(
                """
                INSERT INTO sessions(session_id, created_at, last_seen_at)
                VALUES(?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET last_seen_at=excluded.last_seen_at
                """,
                (session_id, now, now),
            )
            self._conn.commit()

    # ----------------------------- conversation -----------------------------

    def create_conversation(
        self,
        *,
        session_id: str,
        conversation_id: Optional[str] = None,
        title: Optional[str],
        workspace_root: str,
        expo_root: str,
    ) -> str:
        with self._write_lock:
            now = self._now()
            conv_id = conversation_id or uuid4().hex
            self._conn.execute(
                """
                INSERT INTO conversations(
                    conversation_id,
                    session_id,
                    title,
                    created_at,
                    updated_at,
                    workspace_root,
                    expo_root
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (conv_id, session_id, title, now, now, workspace_root, expo_root),
            )
            self._conn.commit()
            return conv_id

    def list_conversations(self, *, session_id: str) -> list[dict[str, Any]]:
        # sqlite3.Connection.execute 返回的是 cursor；fetchall() 需要在 cursor 上调用。
        cur = self._conn.execute(
            "SELECT conversation_id, title, created_at, updated_at FROM conversations WHERE session_id=? ORDER BY updated_at DESC",
            (session_id,),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def get_conversation_or_none(
        self, *, session_id: str, conversation_id: str
    ) -> Optional[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT conversation_id, session_id, title, created_at, updated_at, workspace_root, expo_root
            FROM conversations
            WHERE session_id=? AND conversation_id=?
            """,
            (session_id, conversation_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def set_conversation_title(self, *, conversation_id: str, title: str) -> None:
        with self._write_lock:
            now = self._now()
            self._conn.execute(
                """
                UPDATE conversations SET title=?, updated_at=? WHERE conversation_id=?
                """,
                (title, now, conversation_id),
            )
            self._conn.commit()

    def touch_conversation(self, conversation_id: str) -> None:
        with self._write_lock:
            now = self._now()
            self._conn.execute(
                "UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                (now, conversation_id),
            )
            self._conn.commit()

    # ----------------------------- messages -----------------------------

    def append_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        tool_call_id: Optional[str] = None,
        tool_calls: Optional[Iterable[dict[str, Any]]] = None,
    ) -> int:
        with self._write_lock:
            now = self._now()
            tool_calls_json = (
                json.dumps(list(tool_calls), ensure_ascii=False)
                if tool_calls is not None
                else None
            )
            cur = self._conn.execute(
                """
                INSERT INTO messages(conversation_id, role, content, tool_call_id, tool_calls_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, role, content, tool_call_id, tool_calls_json, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_messages(
        self,
        *,
        conversation_id: str,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        if limit is None:
            cur = self._conn.execute(
                """
                SELECT message_seq, role, content, tool_call_id, tool_calls_json, created_at
                FROM messages
                WHERE conversation_id=?
                ORDER BY message_seq ASC
                """,
                (conversation_id,),
            )
        else:
            cur = self._conn.execute(
                """
                SELECT message_seq, role, content, tool_call_id, tool_calls_json, created_at
                FROM messages
                WHERE conversation_id=?
                ORDER BY message_seq DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            )
            rows = cur.fetchall()[::-1]
            return [dict(r) for r in rows]

        return [dict(r) for r in cur.fetchall()]

    # ----------------------------- runs & events -----------------------------

    def create_run(
        self,
        *,
        conversation_id: str,
        run_id: Optional[str] = None,
        input_text: str,
        status: str,
    ) -> str:
        with self._write_lock:
            run = run_id or uuid4().hex
            now = self._now()
            self._conn.execute(
                """
                INSERT INTO runs(run_id, conversation_id, input_text, status, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (run, conversation_id, input_text, status, now),
            )
            self._conn.commit()
            return run

    def set_run_status(
        self,
        *,
        run_id: str,
        status: str,
        error: Optional[str] = None,
        final_text: Optional[str] = None,
        expo_url: Optional[str] = None,
    ) -> None:
        with self._write_lock:
            now = self._now()
            self._conn.execute(
                """
                UPDATE runs
                SET status=?,
                    finished_at=CASE WHEN ? IN ('completed','failed') THEN ? ELSE finished_at END,
                    error=?,
                    final_text=COALESCE(?, final_text),
                    expo_url=COALESCE(?, expo_url)
                WHERE run_id=?
                """,
                (status, status, now, error, final_text, expo_url, run_id),
            )
            self._conn.commit()

    def get_run_or_none(self, *, run_id: str) -> Optional[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT run_id, conversation_id, input_text, status, created_at, finished_at, error, final_text, expo_url
            FROM runs WHERE run_id=?
            """,
            (run_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_running_run_for_conversation_or_none(
        self, *, conversation_id: str
    ) -> Optional[dict[str, Any]]:
        """查询某个 conversation 当前是否已有 running 的 run。"""

        cur = self._conn.execute(
            """
            SELECT run_id, conversation_id, input_text, status, created_at, finished_at, error, final_text, expo_url
            FROM runs
            WHERE conversation_id=? AND status=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (conversation_id, self.status.running),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def append_agent_event(
        self,
        *,
        run_id: str,
        step_id: int,
        type: str,
        title: str,
        detail: Optional[str] = None,
    ) -> int:
        with self._write_lock:
            now = self._now()
            cur = self._conn.execute(
                """
                INSERT INTO agent_events(run_id, step_id, type, title, detail, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (run_id, step_id, type, title, detail, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_agent_events_incremental(
        self,
        *,
        run_id: str,
        last_step_id: int,
        last_event_seq: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        if last_event_seq is None:
            cur = self._conn.execute(
                """
                SELECT event_seq, step_id, type, title, detail, created_at
                FROM agent_events
                WHERE run_id=? AND step_id > ?
                ORDER BY step_id ASC, event_seq ASC
                """,
                (run_id, last_step_id),
            )
            return [dict(r) for r in cur.fetchall()]

        cur = self._conn.execute(
            """
            SELECT event_seq, step_id, type, title, detail, created_at
            FROM agent_events
            WHERE run_id=? AND (
                step_id > ? OR (step_id = ? AND event_seq > ?)
            )
            ORDER BY step_id ASC, event_seq ASC
            """,
            (run_id, last_step_id, last_step_id, last_event_seq),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_max_event_seq_for_step(
        self, *, run_id: str, step_id: int
    ) -> Optional[int]:
        """用于 WS 增量补发：当客户端只带 lastStepId 时，可推导该 step 内最后收到的 event_seq。"""
        cur = self._conn.execute(
            """
            SELECT MAX(event_seq) as max_event_seq
            FROM agent_events
            WHERE run_id=? AND step_id=?
            """,
            (run_id, step_id),
        )
        row = cur.fetchone()
        max_event_seq = row["max_event_seq"] if row else None
        return int(max_event_seq) if max_event_seq is not None else None

    # ----------------------------- summary memory -----------------------------

    def get_conversation_summary_or_none(
        self, *, conversation_id: str
    ) -> Optional[dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT conversation_id, summary_text, updated_at
            FROM conversation_summaries
            WHERE conversation_id=?
            """,
            (conversation_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def upsert_conversation_summary(
        self, *, conversation_id: str, summary_text: str
    ) -> None:
        with self._write_lock:
            now = self._now()
            self._conn.execute(
                """
                INSERT INTO conversation_summaries(conversation_id, summary_text, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET summary_text=excluded.summary_text, updated_at=excluded.updated_at
                """,
                (conversation_id, summary_text, now),
            )
            self._conn.commit()

    # ----------------------------- cleanup -----------------------------

    def list_expired_conversations(
        self, *, cutoff_ts: int
    ) -> list[dict[str, Any]]:
        """列出 updated_at 早于 cutoff_ts 的 conversation。"""

        cur = self._conn.execute(
            """
            SELECT conversation_id, workspace_root
            FROM conversations
            WHERE updated_at < ?
            ORDER BY updated_at ASC
            """,
            (cutoff_ts,),
        )
        return [dict(r) for r in cur.fetchall()]

    def delete_conversation_data(self, *, conversation_id: str) -> None:
        """删除 conversation 相关数据（messages/runs/agent_events 等）。"""

        # 按外键依赖顺序手动删除，避免约束报错
        with self._write_lock:
            self._conn.execute(
                """
                DELETE FROM agent_events
                WHERE run_id IN (SELECT run_id FROM runs WHERE conversation_id=?)
                """,
                (conversation_id,),
            )
            self._conn.execute(
                "DELETE FROM runs WHERE conversation_id=?",
                (conversation_id,),
            )
            self._conn.execute(
                "DELETE FROM messages WHERE conversation_id=?",
                (conversation_id,),
            )
            self._conn.execute(
                "DELETE FROM conversation_summaries WHERE conversation_id=?",
                (conversation_id,),
            )
            self._conn.execute(
                "DELETE FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            )
            self._conn.commit()

