"""
Auto-restart watchdog — background daemon thread.

Every POLL_INTERVAL seconds, checks all sessions marked 'running' in the DB
and verifies their process is still alive via os.kill(pid, 0).
If the process is gone, the DB record is updated to 'stopped'.

Records with no pid are marked stopped immediately since they cannot be verified.

Sessions on a remote host are checked with one batched ssh call per host. If the
host cannot be reached the sweep skips it entirely — an unreachable host means
"unknown", and marking live sessions stopped over a brief network blip would
lose the very thing rcpilot exists to keep.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from loguru import logger

import pilot.db as db
from pilot import hosts as host_mgr
from pilot import sessions as session_mgr

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
    running = [r for r in db.get_all_running_sessions(db_path) if not r.get("imported")]

    by_host: dict[str, list[dict]] = {}
    for record in running:
        host_name, _ = host_mgr.split_key(record["project"])
        by_host.setdefault(host_name, []).append(record)

    for record in by_host.pop("", []):
        pid = record.get("pid")
        if pid and _pid_alive(pid):
            continue
        logger.info(
            "watchdog: pid {} gone — marking session {} stopped",
            pid,
            record["id"],
        )
        db.mark_session_stopped(db_path, record["id"])

    hosts = {h.name: h for h in config.hosts}
    for host_name, records in by_host.items():
        host = hosts.get(host_name)
        if host is None:
            # The host was removed from config; its sessions are unverifiable.
            logger.debug("watchdog: no config for host {!r} — skipping its sessions", host_name)
            continue
        status = session_mgr.probe_remote(host, records)
        if status is None:
            continue  # unreachable → unknown, not dead
        for record in records:
            if status.get(record["id"], {}).get("alive"):
                continue
            logger.info(
                "watchdog: unit {} inactive on {} — marking session {} stopped",
                record.get("unit"), host_name, record["id"],
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
