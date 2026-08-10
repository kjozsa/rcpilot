"""Tests for sessions living on a remote host."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from pilot import hosts as host_mgr
from pilot import sessions as session_mgr
from pilot.config import Config, RemoteHost
from pilot.db import create_session, get_running_session, init_db, list_sessions
from pilot.watchdog import _sweep

HOST = RemoteHost(name="stardust", ssh="me@stardust")


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    db_path = tmp_path / "test.db"
    asyncio.run(init_db(str(db_path)))
    return Config(
        projects_dir=tmp_path / "projects",
        db_path=db_path,
        hosts=[HOST],
    )


def _fake_script(stdout: str, returncode: int = 0):
    def run_script(host, script, timeout=20.0):
        return subprocess.CompletedProcess([], returncode, stdout, "")
    return run_script


# ---------------------------------------------------------------------------
# Spawning
# ---------------------------------------------------------------------------

def test_start_remote_session_captures_url(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def run_script(host, script, timeout=20.0):
        captured["script"] = script
        return subprocess.CompletedProcess(
            [], 0,
            "Running as unit\nContinue in https://claude.ai/code?environment=env_abc123\n"
            "https://claude.ai/code/session_01abc?from=cli\n",
            "",
        )

    monkeypatch.setattr(host_mgr, "run_script", run_script)

    result = session_mgr.start_session(
        project="stardust:rcpilot",
        project_path="/home/me/projects/rcpilot",
        db_name="afternoon",
        claude_name="rcpilot - afternoon",
        db_path=str(cfg.db_path),
        host=HOST,
    )

    assert result["status"] == "running"
    assert result["rc_url"] == "https://claude.ai/code/session_01abc"

    # Detached from the ssh connection, and pointed at the right directory.
    assert "systemd-run --user --collect --unit=rcpilot-session-" in captured["script"]
    assert "cd /home/me/projects/rcpilot" in captured["script"]
    assert "claude remote-control --spawn=same-dir" in captured["script"]

    row = get_running_session(str(cfg.db_path), "stardust:rcpilot")
    assert row is not None
    assert row["unit"].startswith("rcpilot-session-")
    assert row["unit"].endswith(".service")
    assert row["pid"] is None


def test_start_remote_session_clears_bridge_pointer(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this the next session reattaches to the previous environment URL."""
    captured: dict = {}

    def run_script(host, script, timeout=20.0):
        captured["script"] = script
        return subprocess.CompletedProcess([], 0, "https://claude.ai/code/session_01e1", "")

    monkeypatch.setattr(host_mgr, "run_script", run_script)
    session_mgr.start_session(
        "stardust:rcpilot", "/home/me/projects/rcpilot", "s", "n",
        str(cfg.db_path), host=HOST,
    )
    assert (
        "rm -f ~/.claude/projects/-home-me-projects-rcpilot/bridge-pointer.json"
        in captured["script"]
    )


def test_start_remote_session_reports_spawn_failure(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        host_mgr, "run_script",
        _fake_script("RCPILOT_SPAWN_FAILED Failed to connect to bus\n"),
    )
    result = session_mgr.start_session(
        "stardust:rcpilot", "/home/me/projects/rcpilot", "s", "n",
        str(cfg.db_path), host=HOST,
    )
    assert result["status"] == "error"
    assert "bus" in result["detail"]
    # No half-registered row left behind.
    assert get_running_session(str(cfg.db_path), "stardust:rcpilot") is None


def test_start_remote_session_times_out_without_url(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_mgr, "run_script", _fake_script("nothing useful here\n"))
    result = session_mgr.start_session(
        "stardust:rcpilot", "/home/me/projects/rcpilot", "s", "n",
        str(cfg.db_path), host=HOST,
    )
    assert result["status"] == "timed_out"
    rows = list_sessions(str(cfg.db_path), "stardust:rcpilot")
    assert rows[0]["status"] == "timed_out"


def test_yolo_sets_bypass_permissions_remotely(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def run_script(host, script, timeout=20.0):
        captured["script"] = script
        return subprocess.CompletedProcess([], 0, "https://claude.ai/code/session_01e1", "")

    monkeypatch.setattr(host_mgr, "run_script", run_script)
    session_mgr.start_session(
        "stardust:rcpilot", "/home/me/projects/rcpilot", "s", "n",
        str(cfg.db_path), yolo=True, host=HOST,
    )
    assert "--permission-mode bypassPermissions" in captured["script"]


# ---------------------------------------------------------------------------
# Attach URL — session deep link vs environment URL
# ---------------------------------------------------------------------------

ENV_URL = "https://claude.ai/code?environment=env_01env"
SES_URL = "https://claude.ai/code/session_01aaa"


def test_extract_urls_separates_the_two_forms() -> None:
    session, env = session_mgr._extract_urls(
        f"Continue coding in the Claude mobile app or {ENV_URL}\n{SES_URL}?from=cli\n"
    )
    assert session == SES_URL
    assert env == ENV_URL


def test_extract_urls_takes_the_newest_session() -> None:
    """Extra on-demand sessions appear later in the log; the last one is live."""
    session, _ = session_mgr._extract_urls(
        "https://claude.ai/code/session_01old?from=cli\n"
        "https://claude.ai/code/session_01new?from=cli\n"
    )
    assert session == "https://claude.ai/code/session_01new"


def test_extract_urls_handles_missing_session_link() -> None:
    assert session_mgr._extract_urls(f"only {ENV_URL} here") == (None, ENV_URL)


def test_remote_start_waits_for_the_session_link(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returning as soon as the environment URL lands is what caused the bug."""
    captured: dict = {}

    def run_script(host, script, timeout=20.0):
        captured["script"] = script
        return subprocess.CompletedProcess([], 0, f"{ENV_URL}\n{SES_URL}?from=cli\n", "")

    monkeypatch.setattr(host_mgr, "run_script", run_script)
    result = session_mgr.start_session(
        "stardust:rcpilot", "/home/me/projects/rcpilot", "s", "n",
        str(cfg.db_path), host=HOST,
    )

    assert "grep -qa 'claude\\.ai/code/session_'" in captured["script"]
    assert result["rc_url"] == SES_URL

    row = get_running_session(str(cfg.db_path), "stardust:rcpilot")
    assert row["rc_url"] == SES_URL
    assert row["env_url"] == ENV_URL


def test_remote_start_falls_back_to_environment_url(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better a URL that opens the wrong session than no attach button at all."""
    monkeypatch.setattr(host_mgr, "run_script", _fake_script(f"{ENV_URL}\n"))
    result = session_mgr.start_session(
        "stardust:rcpilot", "/home/me/projects/rcpilot", "s", "n",
        str(cfg.db_path), host=HOST,
    )
    assert result["status"] == "running"
    assert result["rc_url"] == ENV_URL


def test_remote_start_reports_why_the_session_was_refused(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge hides this in its debug file; the terminal output stays green."""
    monkeypatch.setattr(host_mgr, "run_script", _fake_script(
        f"{ENV_URL}\n\n{session_mgr._REMOTE_DEBUG_SEP}\n"
        "2026-08-10T08:22:41.289Z [DEBUG] [bridge] Session creation failed with "
        "status 400: GitHub repository access check failed — re-authorize GitHub in settings\n"
    ))
    result = session_mgr.start_session(
        "stardust:rcpilot", "/home/me/projects/rcpilot", "s", "n",
        str(cfg.db_path), host=HOST,
    )
    assert result["rc_url"] == ENV_URL
    assert result["warning"] == (
        "GitHub repository access check failed — re-authorize GitHub in settings"
    )


def test_spawn_failure_reason_ignores_healthy_logs() -> None:
    assert session_mgr._spawn_failure_reason("[bridge:init] Created initial session s_1") is None


def test_debug_file_path_keeps_tilde_expandable(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully quoted '~/…' makes claude write into a literal '~' dir in the repo."""
    captured: dict = {}

    def run_script(host, script, timeout=20.0):
        captured["script"] = script
        return subprocess.CompletedProcess([], 0, SES_URL, "")

    monkeypatch.setattr(host_mgr, "run_script", run_script)
    session_mgr.start_session(
        "stardust:rcpilot", "/home/me/projects/rcpilot", "s", "n",
        str(cfg.db_path), host=HOST,
    )
    assert "--debug-file ~/.cache/rcpilot/session-" in captured["script"]
    assert "--debug-file '~/" not in captured["script"]


def test_listing_repairs_a_stored_environment_url(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sessions started before the fix get upgraded in place, not left broken."""
    sid = create_session(
        str(cfg.db_path), "stardust:rcpilot", "work", None, ENV_URL,
        unit="rcpilot-session-aaa.service", log_path="~/.cache/rcpilot/a.log",
        env_url=ENV_URL,
    )
    monkeypatch.setattr(
        host_mgr, "run_script", _fake_script(f"{sid}\tactive\t0\t{SES_URL}\n")
    )

    sessions = session_mgr.list_running_sessions("stardust:rcpilot", str(cfg.db_path), host=HOST)
    assert sessions[0]["rc_url"] == SES_URL
    # Persisted, so the repair happens once rather than on every poll.
    assert get_running_session(str(cfg.db_path), "stardust:rcpilot")["rc_url"] == SES_URL


def test_listing_does_not_rewrite_a_good_url(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once attached, the stored session must not drift to a newer one."""
    sid = create_session(
        str(cfg.db_path), "stardust:rcpilot", "work", None, SES_URL,
        unit="rcpilot-session-aaa.service", log_path="~/.cache/rcpilot/a.log",
    )
    monkeypatch.setattr(
        host_mgr, "run_script",
        _fake_script(f"{sid}\tactive\t0\thttps://claude.ai/code/session_01other\n"),
    )

    sessions = session_mgr.list_running_sessions("stardust:rcpilot", str(cfg.db_path), host=HOST)
    assert sessions[0]["rc_url"] == SES_URL


def test_local_listing_repairs_url_from_its_log(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local sessions take the same repair path, reading their own log file."""
    log = tmp_path / "session-local.log"
    log.write_text(f"Continue coding in the Claude mobile app or {ENV_URL}\n{SES_URL}?from=cli\n")
    create_session(
        str(cfg.db_path), "rcpilot", "work", 4242, ENV_URL,
        log_path=str(log), env_url=ENV_URL,
    )
    monkeypatch.setattr(session_mgr, "_pid_alive", lambda _: True)

    sessions = session_mgr.list_running_sessions("rcpilot", str(cfg.db_path))
    assert sessions[0]["rc_url"] == SES_URL
    assert get_running_session(str(cfg.db_path), "rcpilot")["rc_url"] == SES_URL


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------

def test_probe_remote_reads_unit_state(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        {"id": 1, "unit": "rcpilot-session-aaa.service", "log_path": "~/.cache/rcpilot/a.log"},
        {"id": 2, "unit": "rcpilot-session-bbb.service", "log_path": "~/.cache/rcpilot/b.log"},
    ]
    monkeypatch.setattr(
        host_mgr, "run_script",
        _fake_script(
            "1\tactive\t1754812800\thttps://claude.ai/code/session_01aaa\n"
            "2\tinactive\t0\t\n"
        ),
    )
    status = session_mgr.probe_remote(HOST, records)
    assert status == {
        1: {"alive": True, "mtime": 1754812800.0,
            "session_url": "https://claude.ai/code/session_01aaa"},
        2: {"alive": False, "mtime": 0.0, "session_url": None},
    }


def test_probe_remote_returns_unknown_when_host_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise host_mgr.HostUnreachable("connection refused")

    monkeypatch.setattr(host_mgr, "run_script", boom)
    assert session_mgr.probe_remote(HOST, [{"id": 1, "unit": "u.service", "log_path": "x"}]) is None


def test_probe_remote_returns_unknown_on_partial_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated reply must not be read as 'the missing ones are dead'."""
    records = [
        {"id": 1, "unit": "a.service", "log_path": "x"},
        {"id": 2, "unit": "b.service", "log_path": "y"},
    ]
    monkeypatch.setattr(host_mgr, "run_script", _fake_script("1\tactive\t0\t\n"))
    assert session_mgr.probe_remote(HOST, records) is None


def test_list_running_keeps_sessions_when_host_unreachable(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network blip must never garbage-collect a live session."""
    create_session(
        str(cfg.db_path), "stardust:rcpilot", "work", None, "https://claude.ai/code?environment=e",
        unit="rcpilot-session-aaa.service", log_path="~/.cache/rcpilot/a.log",
    )

    def boom(*args, **kwargs):
        raise host_mgr.HostUnreachable("no route to host")

    monkeypatch.setattr(host_mgr, "run_script", boom)

    sessions = session_mgr.list_running_sessions("stardust:rcpilot", str(cfg.db_path), host=HOST)
    assert [s["name"] for s in sessions] == ["work"]
    assert get_running_session(str(cfg.db_path), "stardust:rcpilot")["status"] == "running"


def test_list_running_drops_session_whose_unit_died(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = create_session(
        str(cfg.db_path), "stardust:rcpilot", "work", None, "url",
        unit="rcpilot-session-aaa.service", log_path="~/.cache/rcpilot/a.log",
    )
    monkeypatch.setattr(host_mgr, "run_script", _fake_script(f"{sid}\tinactive\t0\t\n"))

    assert session_mgr.list_running_sessions("stardust:rcpilot", str(cfg.db_path), host=HOST) == []
    assert list_sessions(str(cfg.db_path), "stardust:rcpilot")[0]["status"] == "stopped"


# ---------------------------------------------------------------------------
# Killing
# ---------------------------------------------------------------------------

def test_kill_remote_stops_unit_and_stores_snapshot(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pilot.db import get_session_snapshot

    sid = create_session(
        str(cfg.db_path), "stardust:rcpilot", "work", None, "url",
        unit="rcpilot-session-aaa.service", log_path="~/.cache/rcpilot/a.log",
    )
    captured: dict = {}

    def run_script(host, script, timeout=20.0):
        captured["script"] = script
        return subprocess.CompletedProcess([], 0, "final terminal output\n", "")

    monkeypatch.setattr(host_mgr, "run_script", run_script)

    assert session_mgr.kill_session(sid, str(cfg.db_path), host=HOST) == {"status": "stopped"}
    assert "systemctl --user stop rcpilot-session-aaa.service" in captured["script"]
    assert get_session_snapshot(str(cfg.db_path), sid).strip() == "final terminal output"
    assert list_sessions(str(cfg.db_path), "stardust:rcpilot")[0]["status"] == "stopped"


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def test_watchdog_skips_unreachable_host(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    create_session(
        str(cfg.db_path), "stardust:rcpilot", "work", None, "url",
        unit="rcpilot-session-aaa.service", log_path="~/.cache/rcpilot/a.log",
    )
    monkeypatch.setattr(session_mgr, "probe_remote", lambda *a: None)

    _sweep(cfg)

    assert get_running_session(str(cfg.db_path), "stardust:rcpilot")["status"] == "running"


def test_watchdog_stops_dead_remote_session(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = create_session(
        str(cfg.db_path), "stardust:rcpilot", "work", None, "url",
        unit="rcpilot-session-aaa.service", log_path="~/.cache/rcpilot/a.log",
    )
    monkeypatch.setattr(
        session_mgr, "probe_remote",
        lambda *a: {sid: {"alive": False, "mtime": 0.0, "session_url": None}},
    )

    _sweep(cfg)

    assert list_sessions(str(cfg.db_path), "stardust:rcpilot")[0]["status"] == "stopped"


def test_watchdog_ignores_sessions_of_removed_host(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping a host from config must not silently kill its history."""
    create_session(
        str(cfg.db_path), "ghost:rcpilot", "work", None, "url",
        unit="rcpilot-session-aaa.service",
    )
    monkeypatch.setattr("pilot.watchdog._pid_alive", lambda _: False)

    _sweep(cfg)

    assert get_running_session(str(cfg.db_path), "ghost:rcpilot")["status"] == "running"
