"""Persistent mapping from Discord thread to Antigravity conversation."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  thread_id    TEXT PRIMARY KEY,
  conversation_id   TEXT,
  workspace    TEXT NOT NULL,
  created_by   TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  last_used_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class ThreadRow:
    thread_id: str
    conversation_id: str | None
    workspace: str
    created_by: str
    created_at: int
    last_used_at: int


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def create_thread(self, thread_id: str, workspace: str,
                      created_by: str) -> ThreadRow:
        now = int(time.time())
        self._db.execute(
            "INSERT OR IGNORE INTO threads"
            " (thread_id, conversation_id, workspace, created_by,"
            "  created_at, last_used_at)"
            " VALUES (?, NULL, ?, ?, ?, ?)",
            (thread_id, workspace, created_by, now, now),
        )
        self._db.commit()
        row = self.get_thread(thread_id)
        assert row is not None
        return row

    def get_thread(self, thread_id: str) -> ThreadRow | None:
        cur = self._db.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        )
        r = cur.fetchone()
        return None if r is None else ThreadRow(**dict(r))

    def set_conversation(self, thread_id: str, conversation_id: str) -> None:
        self._db.execute(
            "UPDATE threads SET conversation_id = ? WHERE thread_id = ?",
            (conversation_id, thread_id),
        )
        self._db.commit()

    def touch(self, thread_id: str) -> None:
        self._db.execute(
            "UPDATE threads SET last_used_at = ? WHERE thread_id = ?",
            (int(time.time()), thread_id),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
