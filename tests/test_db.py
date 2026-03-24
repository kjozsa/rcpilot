"""Tests for pilot.db — sync SQLite CRUD helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pilot.db import (
    init_db,
    create_session,
    end_session,
    mark_session_stopped,
    get_running_session,
    get_all_running_sessions,
    list_sessions,
)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    """Return path to an initialised temporary database."""
    path = str(tmp_path / "test.db")
    asyncio.run(init_db(path))
    return path


def test_create_session_inserts_row(db: str) -> None:
    sid = create_session(db, "my-project", "https://claude.ai/rc/abc")
    rows = list_sessions(db, "my-project")
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["project"] == "my-project"
    assert rows[0]["rc_url"] == "https://claude.ai/rc/abc"
    assert rows[0]["status"] == "running"


def test_create_session_auto_names_with_iso_date(db: str) -> None:
    create_session(db, "proj", None)
    row = list_sessions(db, "proj")[0]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert row["name"] == today


def test_end_session_sets_ended_at_and_status(db: str) -> None:
    sid = create_session(db, "proj", None)
    end_session(db, sid, "stopped")
    row = list_sessions(db, "proj")[0]
    assert row["status"] == "stopped"
    assert row["ended_at"] is not None


def test_end_session_stores_pane_snapshot(db: str) -> None:
    import sqlite3
    sid = create_session(db, "proj", None)
    end_session(db, sid, "stopped", pane_snapshot="some terminal output")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    logs = conn.execute(
        "SELECT * FROM session_logs WHERE session_id = ?", (sid,)
    ).fetchall()
    conn.close()

    assert len(logs) == 1
    assert logs[0]["role"] == "snapshot"
    assert logs[0]["content"] == "some terminal output"


def test_end_session_no_snapshot_leaves_logs_empty(db: str) -> None:
    import sqlite3
    sid = create_session(db, "proj", None)
    end_session(db, sid, "stopped")  # no snapshot

    conn = sqlite3.connect(db)
    count = conn.execute(
        "SELECT COUNT(*) FROM session_logs WHERE session_id = ?", (sid,)
    ).fetchone()[0]
    conn.close()
    assert count == 0


def test_mark_session_stopped(db: str) -> None:
    sid = create_session(db, "proj", None)
    mark_session_stopped(db, sid)
    row = list_sessions(db, "proj")[0]
    assert row["status"] == "stopped"
    assert row["ended_at"] is not None


def test_get_running_session_returns_none_when_empty(db: str) -> None:
    assert get_running_session(db, "proj") is None


def test_get_running_session_returns_row(db: str) -> None:
    sid = create_session(db, "proj", "https://claude.ai/rc/x")
    row = get_running_session(db, "proj")
    assert row is not None
    assert row["id"] == sid


def test_get_running_session_ignores_stopped(db: str) -> None:
    sid = create_session(db, "proj", None)
    mark_session_stopped(db, sid)
    assert get_running_session(db, "proj") is None


def test_list_sessions_returns_newest_first(db: str) -> None:
    create_session(db, "proj", None)
    create_session(db, "proj", None)
    rows = list_sessions(db, "proj")
    assert rows[0]["id"] > rows[1]["id"]


def test_list_sessions_filters_by_project(db: str) -> None:
    create_session(db, "alpha", None)
    create_session(db, "beta", None)
    assert len(list_sessions(db, "alpha")) == 1
    assert len(list_sessions(db, "beta")) == 1


def test_get_all_running_sessions_cross_project(db: str) -> None:
    create_session(db, "alpha", None)
    create_session(db, "beta", None)
    sid = create_session(db, "gamma", None)
    mark_session_stopped(db, sid)

    running = get_all_running_sessions(db)
    projects = {r["project"] for r in running}
    assert projects == {"alpha", "beta"}
