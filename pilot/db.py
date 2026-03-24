"""
SQLite persistence for pi-lot.

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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT    NOT NULL,
    name        TEXT,              -- auto-set to ISO date at creation
    started_at  TEXT    NOT NULL,  -- ISO-8601 UTC
    ended_at    TEXT,              -- NULL while still running
    status      TEXT    NOT NULL DEFAULT 'running',
    rc_url      TEXT               -- captured Remote-Control URL
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


def create_session(db_path: str, project: str, rc_url: str | None) -> int:
    """Insert a new running session row; return its id."""
    name = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO sessions (project, name, started_at, status, rc_url) "
            "VALUES (?, ?, ?, 'running', ?)",
            (project, name, _now(), rc_url),
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


def get_running_session(db_path: str, project: str) -> dict[str, Any] | None:
    """Return the most recent running session row for *project*, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE project = ? AND status = 'running' "
            "ORDER BY started_at DESC LIMIT 1",
            (project,),
        ).fetchone()
    return dict(row) if row else None


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
            "SELECT * FROM sessions WHERE project = ? ORDER BY started_at DESC",
            (project,),
        ).fetchall()
    return [dict(r) for r in rows]
