"""Tests for spawning and stopping sessions on the local host."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from pilot import db
from pilot import sessions as session_mgr

ALREADY_SERVED = (
    "Error: This folder is already served by a terminal `claude remote-control` "
    "on this device. Stop it first."
)


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "test.db"
    asyncio.run(db.init_db(str(path)))
    return str(path)


def _fake_spawn(monkeypatch: pytest.MonkeyPatch, shell: str) -> list[subprocess.Popen]:
    """Make start_session run *shell* instead of claude: $1 is the session log and
    $MSG the "already served" error (passed by env so its backticks stay inert)."""
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(cmd, **kwargs):
        log = cmd[-1]
        proc = real_popen(["sh", "-c", shell, "sh", log], start_new_session=True,
                          stdin=subprocess.DEVNULL, env={"MSG": ALREADY_SERVED, "PATH": "/usr/bin:/bin"})
        spawned.append(proc)
        return proc

    monkeypatch.setattr(session_mgr.subprocess, "Popen", popen)
    monkeypatch.setattr(session_mgr, "_clear_bridge_pointer", lambda path: None)
    return spawned


def test_exit_reason_takes_last_error_line() -> None:
    assert session_mgr._exit_reason("noise\nError: first\nError: second\n") == "second"
    assert "exited" in session_mgr._exit_reason("nothing useful")


def test_start_fails_fast_when_bridge_exits(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_spawn(monkeypatch, 'echo "Error: Unknown command: claude" > "$1"; exit 127')

    began = time.monotonic()
    result = session_mgr.start_session("proj", str(tmp_path), "s", "proj - s", db_path)

    assert time.monotonic() - began < 10, "should not wait out the URL timeout"
    assert result["status"] == "timed_out"
    assert result["error"] == "Unknown command: claude"
    assert "holder" not in result


def test_start_fails_fast_when_folder_already_served(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Like real claude: print the error, then linger instead of exiting.
    spawned = _fake_spawn(monkeypatch, 'printf "%s\\nExiting in about 60 seconds.\\n" "$MSG" > "$1"; exec sleep 60')
    monkeypatch.setattr(session_mgr, "bridge_holders", lambda path: [])

    began = time.monotonic()
    result = session_mgr.start_session("proj", str(tmp_path), "s", "proj - s", db_path)

    assert time.monotonic() - began < 10, "should not wait out the URL timeout"
    assert result["status"] == "timed_out"
    assert "already served" in result["error"]
    assert result["holder"] is None
    assert spawned[0].poll() is not None, "the lingering bridge is stopped"


def test_start_reports_untracked_holder(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_spawn(monkeypatch, 'printf "%s\\n" "$MSG" > "$1"; exit 1')
    monkeypatch.setattr(
        session_mgr, "bridge_holders",
        lambda path: [{"pid": 4242, "name": "proj - w", "ancestors": [4241, 4240]}],
    )

    result = session_mgr.start_session("proj", str(tmp_path), "s", "proj - s", db_path)

    assert result["holder"] == {"pid": 4242, "name": "proj - w", "tracked": False}


def test_start_reports_tracked_holder_by_its_db_name(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.create_session(db_path, "proj", "morning", 4240, "https://claude.ai/code/session_x")
    _fake_spawn(monkeypatch, 'printf "%s\\n" "$MSG" > "$1"; exit 1')
    monkeypatch.setattr(
        session_mgr, "bridge_holders",
        lambda path: [{"pid": 4242, "name": "proj - morning", "ancestors": [4241, 4240]}],
    )

    result = session_mgr.start_session("proj", str(tmp_path), "s", "proj - s", db_path)

    assert result["holder"] == {"pid": 4242, "name": "morning", "tracked": True}


def test_start_stops_bridge_when_it_cannot_be_recorded(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = _fake_spawn(
        monkeypatch,
        'echo "https://claude.ai/code/session_01abc?from=cli" > "$1"; exec sleep 60',
    )

    def full(*args, **kwargs):
        raise db.sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(session_mgr.db, "create_session", full)

    with pytest.raises(db.sqlite3.OperationalError):
        session_mgr.start_session("proj", str(tmp_path), "s", "proj - s", db_path)

    assert spawned[0].poll() is not None, "an unrecorded bridge must not be left running"


def test_terminate_waits_for_the_whole_tree() -> None:
    # A child in a session of its own, like claude under `script`: signalling
    # the parent's process group alone would miss it.
    proc = subprocess.Popen(
        ["sh", "-c", "setsid sleep 60 & echo $!; wait"],
        stdout=subprocess.PIPE, start_new_session=True, text=True,
    )
    grandchild = int(proc.stdout.readline())

    session_mgr._terminate(proc.pid)

    assert proc.poll() is not None
    stat = session_mgr._proc_stat(grandchild)
    assert stat is None or stat[0] == "Z"


def test_release_bridge_refuses_unknown_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mgr, "bridge_holders", lambda path: [])
    killed: list[int] = []
    monkeypatch.setattr(session_mgr, "_terminate", lambda pid: killed.append(pid))

    assert session_mgr.release_bridge(str(tmp_path), 1) is False
    assert killed == []
