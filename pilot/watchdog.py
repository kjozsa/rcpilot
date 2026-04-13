"""
Auto-restart watchdog — background daemon thread.

Every POLL_INTERVAL seconds, checks all sessions marked 'running' in the DB
and verifies their process is still alive via os.kill(pid, 0).
If the process is gone, the DB record is updated to 'stopped'.

Records with no pid are marked stopped immediately since they cannot be verified.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from loguru import logger

import pilot.db as db

if TYPE_CHECKING:
    from pilot.config import Config

POLL_INTERVAL: float = 10.0  # seconds between watchdog sweeps


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _watchdog_loop(config: "Config", stop_event: threading.Event) -> None:
    logger.info("watchdog started (poll interval {}s)", POLL_INTERVAL)
    while not stop_event.wait(timeout=POLL_INTERVAL):
        try:
            _sweep(config)
        except Exception:
            logger.exception("watchdog sweep failed")
    logger.info("watchdog stopped")


def _sweep(config: "Config") -> None:
    db_path = str(config.db_path)
    running = db.get_all_running_sessions(db_path)
    for record in running:
        if record.get("imported"):
            continue  # imported sessions have no pid — always considered alive
        pid = record.get("pid")
        if pid and _pid_alive(pid):
            continue
        logger.info(
            "watchdog: pid {} gone — marking session {} stopped",
            pid,
            record["id"],
        )
        db.mark_session_stopped(db_path, record["id"])


def start_watchdog(config: "Config") -> tuple[threading.Thread, threading.Event]:
    """Start the watchdog thread. Returns (thread, stop_event)."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_watchdog_loop,
        args=(config, stop_event),
        daemon=True,
        name="pilot-watchdog",
    )
    thread.start()
    return thread, stop_event
