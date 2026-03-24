"""
Session management — spawn / kill / query claude remote-control inside tmux.

Each project maps to exactly one named tmux session: ``{prefix}{project_name}``.
The RC URL is captured by tailing the tmux pane's output looking for the
characteristic "https://..." line that claude remote-control prints on startup.
Session state is persisted to SQLite via pilot.db so it survives server restarts.
"""

from __future__ import annotations

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
    prefix: str,
    db_path: str,
    spawn_mode: str = "same-dir",
) -> dict[str, Any]:
    """
    Spawn ``claude remote-control`` for *project* inside a new (or reused)
    tmux session.  Blocks for up to _URL_WAIT_SECONDS waiting for the RC URL.

    Returns a dict with keys: ``status``, ``rc_url`` (str | None), ``session_id`` (int | None).
    """
    session_name = _tmux_session_name(prefix, project)
    logger.info("start_session: project={} session={}", project, session_name)

    # If a tmux session is already running, return its DB record rather than re-spawning
    if _session_exists(session_name):
        record = db.get_running_session(db_path, project)
        url = (record or {}).get("rc_url")
        sid = (record or {}).get("id")
        logger.info("session already exists, rc_url={}", url)
        return {"status": "running", "rc_url": url, "session_id": sid}

    logger.info("spawning tmux session in {}", project_path)
    subprocess.run(
        [
            "tmux", "new-session",
            "-d",               # detached
            "-s", session_name,
            "-c", project_path, # starting directory
            f"claude remote-control --spawn={spawn_mode}",
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
        sid = db.create_session(db_path, project, None)
        db.end_session(db_path, sid, "timed_out", pane_text)
        return {"status": "timed_out", "rc_url": None, "session_id": sid}

    logger.info("RC URL captured: {}", url)
    sid = db.create_session(db_path, project, url)
    return {"status": "running", "rc_url": url, "session_id": sid}


def get_session_status(project: str, prefix: str, db_path: str) -> dict[str, Any]:
    """
    Return current status and RC URL for *project*.
    Does NOT start a new session.
    """
    session_name = _tmux_session_name(prefix, project)

    if not _session_exists(session_name):
        return {"status": "stopped", "rc_url": None, "session_id": None}

    # tmux session is alive — get URL from DB record (survives pane scroll)
    record = db.get_running_session(db_path, project)
    url = (record or {}).get("rc_url")
    sid = (record or {}).get("id")
    logger.debug("get_session_status: project={} url={}", project, url)
    return {"status": "running", "rc_url": url, "session_id": sid}


def kill_session(project: str, prefix: str, db_path: str) -> dict[str, Any]:
    """
    Kill the tmux session for *project*.
    Captures a pane snapshot before killing and stores it in session_logs.
    """
    session_name = _tmux_session_name(prefix, project)
    logger.info("kill_session: project={} session={}", project, session_name)

    if _session_exists(session_name):
        # Capture pane output before the session dies
        snapshot = _capture_pane(session_name)
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            check=True,
        )
        logger.info("tmux session killed")

        record = db.get_running_session(db_path, project)
        if record:
            db.end_session(db_path, record["id"], "stopped", snapshot)
    else:
        logger.info("no tmux session to kill; checking DB for stale running record")
        record = db.get_running_session(db_path, project)
        if record:
            db.mark_session_stopped(db_path, record["id"])

    return {"status": "stopped", "rc_url": None, "session_id": None}


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
