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

import json
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
_URL_WAIT_SECONDS = 60
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


_ENV_URL_PATTERN = re.compile(r"https://claude\.ai/code\?environment=(env_[A-Za-z0-9]+)")


def _ensure_bridge_pointer(project_path: str, rc_url: str) -> None:
    """
    When a session starts as a new environment (?environment=...), Claude Code
    won't create bridge-pointer.json automatically — it only does so when an
    existing one is already present.  Without the file, every subsequent session
    spawns a fresh environment instead of resuming the previous one, so the
    --name flag never sticks in Claude.ai.

    Create a minimal bridge-pointer.json with the environmentId so that the
    NEXT rcpilot session finds it, connects to the same environment, and can
    apply --name to an existing session.
    """
    env_match = _ENV_URL_PATTERN.search(rc_url)
    if not env_match:
        return  # Session URL (/session_...) — bridge-pointer already present

    env_id = env_match.group(1)
    project_dir_key = project_path.replace("/", "-")
    bridge_path = Path.home() / ".claude" / "projects" / project_dir_key / "bridge-pointer.json"

    if bridge_path.exists():
        return  # Already present — Claude Code manages it from here

    bridge_path.parent.mkdir(parents=True, exist_ok=True)
    bridge_path.write_text(json.dumps({"sessionId": None, "environmentId": env_id, "source": "rcpilot"}))
    logger.info("created bridge-pointer.json for {} env={}", project_path, env_id)


def start_session(
    project: str,
    project_path: str,
    db_name: str,
    claude_name: str,
    db_path: str,
    yolo: bool = False,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """
    Spawn `claude remote-control` via `script`. Blocks until URL is captured or timeout.
    Returns dict with keys: status, rc_url, session_id, name.
    """
    db_dir = Path(db_path).parent
    log_path = db_dir / f"session-{secrets.token_hex(6)}.log"

    claude_cmd = f"claude remote-control --spawn=session --name {claude_name!r}"
    if yolo:
        claude_cmd += " --permission-mode bypassPermissions"

    # Wrap in systemd-run --scope so the process lives in its own transient
    # cgroup, outside the rcpilot service cgroup.  Without this, systemd's
    # default KillMode=control-group would kill Claude when rcpilot restarts —
    # even though script is spawned with start_new_session=True.
    cmd = [
        "systemd-run", "--user", "--scope", "--",
        "script", "-q", "-e", "-f", "-c", claude_cmd, str(log_path),
    ]
    logger.info("start_session: project={} db_name={!r} claude_name={!r} log={}", project, db_name, claude_name, log_path)

    env = None
    if proxy_url:
        env = os.environ.copy()
        env["ANTHROPIC_BASE_URL"] = proxy_url

    proc = subprocess.Popen(
        cmd,
        cwd=project_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info("spawned script pid={}", proc.pid)

    url, output = _poll_log_for_url(log_path, _URL_WAIT_SECONDS)

    sid = db.create_session(
        db_path, project, db_name, proc.pid, url, log_path=str(log_path)
    )

    if url is None:
        logger.warning("timed out waiting for RC URL. Log:\n{}", output.strip() or "(empty)")
        db.end_session(db_path, sid, "timed_out", output or None)
        return {"status": "timed_out", "rc_url": None, "session_id": sid, "name": db_name}

    logger.info("RC URL captured: {}", url)
    _ensure_bridge_pointer(project_path, url)
    return {"status": "running", "rc_url": url, "session_id": sid, "name": db_name}


def list_running_sessions(project: str, db_path: str) -> list[dict[str, Any]]:
    """
    Return all live running sessions for *project*.
    Auto-marks stale DB records (process gone) as stopped.
    Imported sessions (no pid) are always considered alive.
    """
    records = db.list_running_sessions(db_path, project)
    result = []
    for record in records:
        pid = record.get("pid")
        is_imported = bool(record.get("imported"))
        if is_imported or (pid and _pid_alive(pid)):
            result.append({
                "id": record["id"],
                "name": record["name"] or "",
                "rc_url": record["rc_url"],
                "status": "running",
                "imported": is_imported,
            })
        else:
            logger.debug("stale running record {} — pid {} gone, marking stopped", record["id"], pid)
            db.mark_session_stopped(db_path, record["id"])
    return result


def resume_session(
    session_id: int,
    project: str,
    project_path: str,
    db_path: str,
    yolo: bool = False,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """
    Start a new session as a continuation of a previous one.
    Looks up the old session name and prefixes it with 'cont.' for the new session.
    """
    record = db.get_session_by_id(db_path, session_id)
    if not record:
        return {"status": "error", "rc_url": None, "session_id": None, "name": None}
    old_name = record.get("name") or record.get("started_at", "")[:10]
    db_name = f"cont. {old_name}"
    claude_name = f"{project} - {db_name}"
    return start_session(project, project_path, db_name, claude_name, db_path, yolo=yolo, proxy_url=proxy_url)


def kill_session(session_id: int, db_path: str) -> dict[str, Any]:
    """
    Terminate a session by SIGTERMing the script process group.
    Reads the log file for a snapshot before cleaning up.
    Imported sessions have no pid; they are just marked stopped.
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


def import_session(
    project: str,
    rc_url: str,
    db_name: str,
    db_path: str,
) -> dict[str, Any]:
    """
    Register an externally-started RC session (e.g. from the IDE) by its URL.
    No process is spawned — the session is stored as imported with no pid.
    """
    logger.info("import_session: project={} db_name={!r} rc_url={}", project, db_name, rc_url)
    sid = db.create_session(
        db_path, project, db_name, pid=None, rc_url=rc_url, imported=True
    )
    return {"status": "running", "rc_url": rc_url, "session_id": sid, "name": db_name}
