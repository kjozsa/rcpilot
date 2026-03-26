"""
Session management — spawn / kill / query claude remote-control via `script`.

Each session runs `claude remote-control --spawn=session` inside the `script`
command, which creates and owns a PTY independently of this Python process.
All output is written to a log file. Because `script` is a separate OS process,
sessions survive FastAPI restarts — the log file and pid are persisted in SQLite.

Flow:
  start  → spawn `script -q -e -c "claude ..." {log_path}` detached
           → poll log file until RC URL appears
           → store pid + log_path + url in DB
  list   → check os.kill(pid, 0) for each running DB record
  kill   → SIGTERM the script process group (kills script + claude together)
           → read log file for snapshot
"""

from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from loguru import logger

import pilot.db as db

# Matches the RC session URL printed by `claude remote-control`
_RC_URL_PATTERN = re.compile(r"https://claude\.ai/code[/\?][A-Za-z0-9_=?&%-]+")

# Strip ANSI escape codes (present in PTY output captured by `script`)
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Seconds to wait for the RC URL to appear in the log file
_URL_WAIT_SECONDS = 30
_POLL_INTERVAL = 0.3


def _pid_alive(pid: int) -> bool:
    """Return True if a process with *pid* is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def _poll_log_for_url(log_path: Path, timeout: float) -> tuple[str | None, str]:
    """
    Poll *log_path* until an RC URL is found or *timeout* seconds elapse.
    Returns (url_or_None, full_log_text).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            text = log_path.read_text(errors="replace")
            clean = _strip_ansi(text)
            match = _RC_URL_PATTERN.search(clean)
            if match:
                return match.group(0), clean
        time.sleep(_POLL_INTERVAL)
    text = log_path.read_text(errors="replace") if log_path.exists() else ""
    return None, _strip_ansi(text)


def start_session(
    project: str,
    project_path: str,
    name: str,
    db_path: str,
    yolo: bool = False,
) -> dict[str, Any]:
    """
    Spawn `claude remote-control` via `script`. Blocks until URL is captured or timeout.
    Returns dict with keys: status, rc_url, session_id, name.
    """
    db_dir = Path(db_path).parent
    log_path = db_dir / f"session-{secrets.token_hex(6)}.log"

    claude_cmd = f"claude remote-control --spawn=session --name {name!r}"
    if yolo:
        claude_cmd += " --permission-mode bypassPermissions"

    cmd = ["script", "-q", "-e", "-f", "-c", claude_cmd, str(log_path)]
    logger.info("start_session: project={} name={!r} log={}", project, name, log_path)

    proc = subprocess.Popen(
        cmd,
        cwd=project_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # new session → independent of FastAPI restarts
    )
    logger.info("spawned script pid={}", proc.pid)

    url, output = _poll_log_for_url(log_path, _URL_WAIT_SECONDS)

    sid = db.create_session(
        db_path, project, name, proc.pid, url, log_path=str(log_path)
    )

    if url is None:
        logger.warning("timed out waiting for RC URL. Log:\n{}", output.strip() or "(empty)")
        db.end_session(db_path, sid, "timed_out", output or None)
        return {"status": "timed_out", "rc_url": None, "session_id": sid, "name": name}

    logger.info("RC URL captured: {}", url)
    return {"status": "running", "rc_url": url, "session_id": sid, "name": name}


def list_running_sessions(project: str, db_path: str) -> list[dict[str, Any]]:
    """
    Return all live running sessions for *project*.
    Auto-marks stale DB records (process gone) as stopped.
    """
    records = db.list_running_sessions(db_path, project)
    result = []
    for record in records:
        pid = record.get("pid")
        if pid and _pid_alive(pid):
            result.append({
                "id": record["id"],
                "name": record["name"] or "",
                "rc_url": record["rc_url"],
                "status": "running",
            })
        else:
            logger.debug("stale running record {} — pid {} gone, marking stopped", record["id"], pid)
            db.mark_session_stopped(db_path, record["id"])
    return result


def kill_session(session_id: int, db_path: str) -> dict[str, Any]:
    """
    Terminate a session by SIGTERMing the script process group.
    Reads the log file for a snapshot before cleaning up.
    """
    record = db.get_session_by_id(db_path, session_id)
    if not record:
        logger.warning("kill_session: session {} not found in DB", session_id)
        return {"status": "stopped"}

    pid = record.get("pid")
    log_path_str = record.get("log_path")
    snapshot: str | None = None

    if pid and _pid_alive(pid):
        try:
            # Kill the entire process group (script + claude child)
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            # Give it a moment to flush the log
            time.sleep(0.5)
        except OSError:
            pass

    if log_path_str:
        log_path = Path(log_path_str)
        if log_path.exists():
            snapshot = _strip_ansi(log_path.read_text(errors="replace"))

    db.end_session(db_path, session_id, "stopped", snapshot)
    logger.info("kill_session: id={} pid={}", session_id, pid)
    return {"status": "stopped"}
