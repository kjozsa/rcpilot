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
    """Watchdog marks a session stopped when its process is no longer alive."""
    monkeypatch.setattr("pilot.watchdog._pid_alive", lambda _: False)

    create_session(str(cfg.db_path), "my-project", "test", None, None)
    _sweep(cfg)

    rows = list_sessions(str(cfg.db_path), "my-project")
    assert rows[0]["status"] == "stopped"
    assert rows[0]["ended_at"] is not None


def test_sweep_leaves_live_session_alone(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Watchdog does not touch a session whose process is still running."""
    monkeypatch.setattr("pilot.watchdog._pid_alive", lambda _: True)

    create_session(str(cfg.db_path), "my-project", "test", 12345, None)
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


def test_prune_deletes_stale_logs_but_keeps_running_and_fresh(cfg: Config) -> None:
    """Old session logs are pruned; fresh ones and a running session's files stay."""
    import os
    import time

    from pilot.watchdog import LOG_RETENTION_DAYS, _prune_session_logs

    log_dir = cfg.db_path.parent
    old = time.time() - (LOG_RETENTION_DAYS + 1) * 86400
    stale_log, stale_debug = log_dir / "session-aaa.log", log_dir / "session-aaa.debug"
    running_log, running_debug = log_dir / "session-bbb.log", log_dir / "session-bbb.debug"
    fresh_log = log_dir / "session-ccc.log"
    other = log_dir / "bridge-transcript-x.jsonl"
    for p in (stale_log, stale_debug, running_log, running_debug, fresh_log, other):
        p.write_text("x")
    for p in (stale_log, stale_debug, running_log, running_debug, other):
        os.utime(p, (old, old))
    create_session(str(cfg.db_path), "my-project", "test", 1, None, log_path=str(running_log))

    assert _prune_session_logs(cfg) == 2

    assert not stale_log.exists() and not stale_debug.exists()
    assert running_log.exists() and running_debug.exists()
    assert fresh_log.exists() and other.exists()
