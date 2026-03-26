"""
SQLite persistence for claude-pilot.

Two layers:
  - async init_db()       — runs once at startup via FastAPI lifespan (aiosqlite)
  - sync CRUD helpers     — called from sync route handlers (stdlib sqlite3)

The split avoids running async code inside thread-pool route handlers while
still letting the startup path stay properly async.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import aiosqlite

# ---------------------------------------------------------------------------
# Schema DDL — used by both init_db and tests
# ---------------------------------------------------------------------------

CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project       TEXT    NOT NULL,
    name          TEXT,
    pid           INTEGER,           -- OS pid of the `script` process holding the PTY
    log_path      TEXT,              -- path to the session log file written by `script`
    started_at    TEXT    NOT NULL,  -- ISO-8601 UTC
    ended_at      TEXT,              -- NULL while still running
    status        TEXT    NOT NULL DEFAULT 'running',
    rc_url        TEXT               -- captured Remote-Control URL
)
"""

CREATE_SESSION_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS session_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    recorded_at TEXT    NOT NULL,  -- ISO-8601 UTC
    role        TEXT    NOT NULL,  -- 'user' | 'assistant' | 'snapshot'
    content     TEXT    NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Async init — called once at startup
# ---------------------------------------------------------------------------

async def init_db(db_path: str) -> None:
    """Create the database file and apply the schema if not already present."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(CREATE_SESSIONS_TABLE)
        await db.execute(CREATE_SESSION_LOGS_TABLE)
        cols = await db.execute("PRAGMA table_info(sessions)")
        col_names = [row[1] for row in await cols.fetchall()]
        if "pid" not in col_names:
            await db.execute("ALTER TABLE sessions ADD COLUMN pid INTEGER")
        if "log_path" not in col_names:
            await db.execute("ALTER TABLE sessions ADD COLUMN log_path TEXT")
        await db.commit()


# ---------------------------------------------------------------------------
# Sync CRUD helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # rows accessible as dicts
    return conn


def create_session(
    db_path: str,
    project: str,
    name: str,
    pid: int | None,
    rc_url: str | None,
    log_path: str | None = None,
) -> int:
    """Insert a new running session row; return its id."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO sessions (project, name, pid, log_path, started_at, status, rc_url) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (project, name, pid, log_path, _now(), rc_url),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def end_session(
    db_path: str,
    session_id: int,
    status: str,
    pane_snapshot: str | None = None,
) -> None:
    """Mark a session ended and optionally store a raw pane snapshot."""
    ended_at = _now()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ?, status = ? WHERE id = ?",
            (ended_at, status, session_id),
        )
        if pane_snapshot is not None:
            conn.execute(
                "INSERT INTO session_logs (session_id, recorded_at, role, content) "
                "VALUES (?, ?, 'snapshot', ?)",
                (session_id, ended_at, pane_snapshot),
            )
        conn.commit()


def mark_session_stopped(db_path: str, session_id: int) -> None:
    """Convenience wrapper used by the watchdog to mark a dead session stopped."""
    end_session(db_path, session_id, status="stopped")


def get_session_by_id(db_path: str, session_id: int) -> dict[str, Any] | None:
    """Return a single session row by ID, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def list_running_sessions(db_path: str, project: str) -> list[dict[str, Any]]:
    """Return all running session rows for *project*, newest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE project = ? AND status = 'running' "
            "ORDER BY started_at DESC",
            (project,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_running_sessions(db_path: str) -> list[dict[str, Any]]:
    """Return all running session rows across all projects (used by watchdog)."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status = 'running'"
        ).fetchall()
    return [dict(r) for r in rows]


def list_sessions(db_path: str, project: str) -> list[dict[str, Any]]:
    """Return all session rows for *project*, newest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM session_logs sl "
            " WHERE sl.session_id = s.id AND sl.role = 'snapshot') AS has_snapshot "
            "FROM sessions s WHERE s.project = ? AND s.status != 'running' ORDER BY s.started_at DESC",
            (project,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_session_snapshot(db_path: str, session_id: int) -> str | None:
    """Return the pane snapshot content for a session, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT content FROM session_logs "
            "WHERE session_id = ? AND role = 'snapshot' LIMIT 1",
            (session_id,),
        ).fetchone()
    return row["content"] if row else None


def rename_session(db_path: str, session_id: int, name: str) -> bool:
    """Update the name of a session. Returns True if a row was updated."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE sessions SET name = ? WHERE id = ?",
            (name, session_id),
        )
        conn.commit()
    return cur.rowcount > 0


def clear_history(db_path: str, project: str) -> int:
    """Delete all non-running sessions (and their logs) for *project*.

    Returns the number of sessions deleted.
    """
    with _connect(db_path) as conn:
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM sessions WHERE project = ? AND status != 'running'",
                (project,),
            ).fetchall()
        ]
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM session_logs WHERE session_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", ids)
            conn.commit()
    return len(ids)
