"""Tests for the watchdog background thread."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pilot.config import Config
from pilot.db import create_session, get_running_session, list_sessions, init_db
from pilot.watchdog import start_watchdog, _sweep


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    db_path = tmp_path / "test.db"
    asyncio.run(init_db(str(db_path)))
    return Config(
        projects_dir=tmp_path / "projects",
        db_path=db_path,
    )


def test_sweep_marks_dead_session_stopped(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Watchdog marks a session stopped when its tmux session no longer exists."""
    monkeypatch.setattr("pilot.watchdog._session_exists", lambda _: False)

    sid = create_session(str(cfg.db_path), "my-project", None)
    _sweep(cfg)

    rows = list_sessions(str(cfg.db_path), "my-project")
    assert rows[0]["status"] == "stopped"
    assert rows[0]["ended_at"] is not None


def test_sweep_leaves_live_session_alone(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Watchdog does not touch a session whose tmux session is still running."""
    monkeypatch.setattr("pilot.watchdog._session_exists", lambda _: True)

    sid = create_session(str(cfg.db_path), "my-project", None)
    _sweep(cfg)

    row = get_running_session(str(cfg.db_path), "my-project")
    assert row is not None
    assert row["status"] == "running"


def test_sweep_handles_no_running_sessions(cfg: Config) -> None:
    """Sweep with an empty DB does not raise."""
    _sweep(cfg)  # should not raise


def test_start_watchdog_thread_is_daemon(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The watchdog thread is a daemon so it exits with the main process."""
    monkeypatch.setattr("pilot.watchdog.POLL_INTERVAL", 999.0)
    thread, stop_event = start_watchdog(cfg)
    assert thread.daemon is True
    stop_event.set()
    thread.join(timeout=2)
