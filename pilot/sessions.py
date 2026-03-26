"""
Session management — spawn / kill / query claude remote-control inside tmux.

Each session gets its own uniquely named tmux session: ``{prefix}{project}-{hex}``,
allowing multiple concurrent sessions per project.
Session state is persisted to SQLite via pilot.db so it survives server restarts.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Literal

from loguru import logger

import pilot.db as db

# Matches the RC session URL that `claude remote-control` prints.
# Known formats (both observed in the wild):
#   https://claude.ai/code/session_01VG9LMVdzoPXv6FLKEk4Mwf   (session path)
#   https://claude.ai/code?bridge=env_01Abc...                 (bridge query param)
# The token may wrap across lines at the terminal width, so we join lines
# before matching (see _extract_rc_url).
_RC_URL_PATTERN = re.compile(r"https://claude\.ai/code[/\?][A-Za-z0-9_=?&-]+")

SessionStatus = Literal["running", "stopped", "timed_out"]

# How many seconds to wait for the RC URL to appear in tmux output
_URL_WAIT_SECONDS = 30
_POLL_INTERVAL = 0.5


def _tmux_session_name(prefix: str, project: str) -> str:
    # Replace characters that tmux treats as special in session names
    safe = project.replace(":", "_").replace(".", "_")
    return f"{prefix}{safe}"


def _session_exists(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def _capture_pane(session_name: str) -> str:
    """Return the visible text of the first pane in the given tmux session."""
    result = subprocess.run(
        # -p: print to stdout; -S -: start from oldest history line
        ["tmux", "capture-pane", "-pt", session_name, "-S", "-"],
        capture_output=True,
        text=True,
    )
    return result.stdout


def start_session(
    project: str,
    project_path: str,
    name: str,
    prefix: str,
    db_path: str,
    yolo: bool = False,
) -> dict[str, Any]:
    """
    Spawn ``claude remote-control`` for *project* inside a new tmux session.
    Each call always creates a fresh session, allowing multiple concurrent sessions.
    Blocks for up to _URL_WAIT_SECONDS waiting for the RC URL.

    Returns a dict with keys: ``status``, ``rc_url`` (str | None), ``session_id`` (int | None), ``name`` (str).
    """
    suffix = os.urandom(3).hex()
    session_name = f"{_tmux_session_name(prefix, project)}-{suffix}"
    logger.info("start_session: project={} session={} name={!r}", project, session_name, name)

    cmd = f"claude remote-control --spawn=session{' --permission-mode bypassPermissions' if yolo else ''}"
    logger.info("spawning tmux session in {} cmd={!r}", project_path, cmd)
    subprocess.run(
        [
            "tmux", "new-session",
            "-d",               # detached
            "-s", session_name,
            "-c", project_path, # starting directory
            cmd,
        ],
        check=True,
    )
    logger.info("tmux session created, waiting up to {}s for RC URL", _URL_WAIT_SECONDS)

    url = _wait_for_rc_url(session_name)
    if url is None:
        pane_text = _capture_pane(session_name)
        logger.warning(
            "timed out waiting for RC URL after {}s. Last pane output:\n{}",
            _URL_WAIT_SECONDS,
            pane_text.strip() or "(empty)",
        )
        sid = db.create_session(db_path, project, name, session_name, None)
        db.end_session(db_path, sid, "timed_out", pane_text)
        return {"status": "timed_out", "rc_url": None, "session_id": sid, "name": name}

    logger.info("RC URL captured: {}", url)
    sid = db.create_session(db_path, project, name, session_name, url)
    return {"status": "running", "rc_url": url, "session_id": sid, "name": name}


def list_running_sessions(project: str, prefix: str, db_path: str) -> list[dict[str, Any]]:
    """
    Return all live running sessions for *project*.
    Cross-checks DB records against actual tmux session existence;
    stale records (tmux gone) are marked stopped automatically.
    """
    records = db.list_running_sessions(db_path, project)
    result = []
    for record in records:
        tmux_name = record.get("tmux_session") or _tmux_session_name(prefix, project)
        if _session_exists(tmux_name):
            result.append({
                "id": record["id"],
                "name": record["name"] or "",
                "rc_url": record["rc_url"],
                "status": "running",
            })
        else:
            logger.debug("stale running record {} — tmux session gone, marking stopped", record["id"])
            db.mark_session_stopped(db_path, record["id"])
    return result


def kill_session(session_id: int, db_path: str) -> dict[str, Any]:
    """
    Kill the tmux session for a specific session ID.
    Captures a pane snapshot before killing and stores it in session_logs.
    """
    record = db.get_session_by_id(db_path, session_id)
    if not record:
        logger.warning("kill_session: session {} not found in DB", session_id)
        return {"status": "stopped"}

    tmux_name = record.get("tmux_session")
    logger.info("kill_session: id={} tmux={}", session_id, tmux_name)

    if tmux_name and _session_exists(tmux_name):
        snapshot = _capture_pane(tmux_name)
        subprocess.run(["tmux", "kill-session", "-t", tmux_name], check=True)
        logger.info("tmux session {} killed", tmux_name)
        db.end_session(db_path, session_id, "stopped", snapshot)
    else:
        db.mark_session_stopped(db_path, session_id)

    return {"status": "stopped"}


def resume_session(
    session_id: int,
    project: str,
    project_path: str,
    name: str,
    prefix: str,
    db_path: str,
    yolo: bool = False,
) -> dict[str, Any]:
    """
    Spawn a new ``claude remote-control`` session that resumes the conversation
    from a prior session.  The session ID is extracted from the stored rc_url;
    if none is found we fall back to ``--resume`` without an explicit ID (which
    resumes the most recent conversation in that project directory).
    """
    record = db.get_session_by_id(db_path, session_id)
    if not record:
        return {"status": "error", "detail": "session not found"}

    # Extract Claude Code session ID from the rc_url, e.g.
    #   https://claude.ai/code/session_01VG9LMVdzoPXv6FLKEk4Mwf  →  session_01VG9…
    claude_session_id: str | None = None
    rc_url = record.get("rc_url") or ""
    match = re.search(r"/code/(session_[A-Za-z0-9]+)", rc_url)
    if match:
        claude_session_id = match.group(1)

    resume_flag = f" --resume {claude_session_id}" if claude_session_id else " --resume"
    yolo_flag = " --permission-mode bypassPermissions" if yolo else ""
    cmd = f"claude remote-control --spawn=session{resume_flag}{yolo_flag}"

    suffix = os.urandom(3).hex()
    session_name = f"{_tmux_session_name(prefix, project)}-{suffix}"
    logger.info("resume_session: prior_id={} project={} session={} cmd={!r}", session_id, project, session_name, cmd)

    subprocess.run(
        [
            "tmux", "new-session",
            "-d",
            "-s", session_name,
            "-c", project_path,
            cmd,
        ],
        check=True,
    )
    logger.info("tmux session created for resume, waiting up to {}s for RC URL", _URL_WAIT_SECONDS)

    url = _wait_for_rc_url(session_name)
    if url is None:
        pane_text = _capture_pane(session_name)
        logger.warning("timed out waiting for RC URL on resume after {}s", _URL_WAIT_SECONDS)
        sid = db.create_session(db_path, project, name, session_name, None)
        db.end_session(db_path, sid, "timed_out", pane_text)
        return {"status": "timed_out", "rc_url": None, "session_id": sid, "name": name}

    logger.info("RC URL captured on resume: {}", url)
    sid = db.create_session(db_path, project, name, session_name, url)
    return {"status": "running", "rc_url": url, "session_id": sid, "name": name}


def send_keys(project: str, prefix: str, db_path: str, text: str) -> bool:
    """Send *text* to the most recently started active tmux session for *project*.

    Returns True if keys were sent, False if no active session found.
    """
    records = db.list_running_sessions(db_path, project)
    for record in records:
        tmux_name = record.get("tmux_session") or _tmux_session_name(prefix, project)
        if _session_exists(tmux_name):
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_name, text, "Enter"],
                check=True,
            )
            logger.info("send_keys: project={} tmux={} text={!r}", project, tmux_name, text[:80])
            return True
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_rc_url(session_name: str) -> str | None:
    """Scan current pane output for an RC URL; return the first match or None."""
    text = _capture_pane(session_name)
    # The bridge token is long enough to wrap at narrow terminal widths.
    # Join lines where a URL-safe character is followed immediately by a newline
    # and then another URL-safe character (no spaces involved).
    text = re.sub(r"(?<=[A-Za-z0-9_=-])\n(?=[A-Za-z0-9_=-])", "", text)
    match = _RC_URL_PATTERN.search(text)
    return match.group(0) if match else None


def _wait_for_rc_url(session_name: str) -> str | None:
    """
    Poll pane output until the RC URL appears or the timeout expires.
    Blocking — intentional: FastAPI sync route handlers run in a thread pool,
    so this won't block the event loop.
    """
    deadline = time.monotonic() + _URL_WAIT_SECONDS
    while time.monotonic() < deadline:
        url = _extract_rc_url(session_name)
        if url:
            return url
        time.sleep(_POLL_INTERVAL)
    return None
