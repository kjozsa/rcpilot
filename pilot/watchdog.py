"""
Auto-restart watchdog — background daemon thread.

Every POLL_INTERVAL seconds, checks all sessions that are marked 'running'
in the DB and verifies their tmux session still exists. If the tmux session
is gone (RC timed out or crashed), the DB record is updated to 'stopped'.

The thread is a daemon so it exits automatically when the main process ends.
A threading.Event is used for the sleep so tests can trigger an immediate
cycle rather than waiting for the full interval.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from loguru import logger

import pilot.db as db
from pilot.sessions import _session_exists, _tmux_session_name

if TYPE_CHECKING:
    from pilot.config import Config

POLL_INTERVAL: float = 10.0  # seconds between watchdog sweeps


def _watchdog_loop(config: "Config", stop_event: threading.Event) -> None:
    logger.info("watchdog started (poll interval {}s)", POLL_INTERVAL)
    while not stop_event.wait(timeout=POLL_INTERVAL):
        try:
            _sweep(config)
        except Exception:
            # Never let an exception kill the watchdog thread
            logger.exception("watchdog sweep failed")
    logger.info("watchdog stopped")


def _sweep(config: "Config") -> None:
    db_path = str(config.db_path)
    running = db.get_all_running_sessions(db_path)
    for record in running:
        session_name = _tmux_session_name(config.tmux_session_prefix, record["project"])
        if not _session_exists(session_name):
            logger.info(
                "watchdog: tmux session '{}' is gone — marking session {} stopped",
                session_name,
                record["id"],
            )
            db.mark_session_stopped(db_path, record["id"])


def start_watchdog(config: "Config") -> tuple[threading.Thread, threading.Event]:
    """
    Start the watchdog thread.

    Returns (thread, stop_event) so callers (and tests) can signal a stop
    or trigger an immediate sweep via stop_event.set().
    """
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_watchdog_loop,
        args=(config, stop_event),
        daemon=True,
        name="pilot-watchdog",
    )
    thread.start()
    return thread, stop_event
