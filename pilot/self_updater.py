"""
Background job that checks PyPI for a newer rcpilot version.

update_mode = "prompt"  → sets update_available flag; UI shows a banner
update_mode = "auto"    → upgrades via uv and restarts the service silently

Checks on startup, then every 6 hours.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from loguru import logger

if TYPE_CHECKING:
    from pilot.config import Config

_CHECK_INTERVAL = 6 * 3600  # seconds between PyPI checks
_PYPI_URL = "https://pypi.org/pypi/rcpilot/json"

_state: dict = {
    "latest_version": None,    # latest version string from PyPI
    "update_available": False,  # True if latest > current
    "last_check_at": None,     # ISO datetime of last check
}
_state_lock = threading.Lock()


def get_self_updater_state() -> dict:
    with _state_lock:
        return dict(_state)


def _parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


def _fetch_latest_version() -> str | None:
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(_PYPI_URL)
            r.raise_for_status()
            return r.json()["info"]["version"]
    except Exception:
        logger.exception("self_updater: failed to fetch PyPI version")
        return None


def _check_for_update(current: str) -> bool:
    latest = _fetch_latest_version()
    now = datetime.now().isoformat(timespec="seconds")
    with _state_lock:
        _state["last_check_at"] = now
        if latest:
            _state["latest_version"] = latest
            available = _parse_version(latest) > _parse_version(current)
            _state["update_available"] = available
            return available
    return False


def _uv_bin() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")


def _run_upgrade() -> bool:
    try:
        result = subprocess.run(
            [_uv_bin(), "tool", "install", "rcpilot", "--upgrade"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info("self_updater: upgrade OK")
            return True
        logger.warning(
            "self_updater: upgrade failed ({}): {}",
            result.returncode,
            result.stderr.strip(),
        )
    except Exception:
        logger.exception("self_updater: upgrade error")
    return False


def _restart_service() -> None:
    subprocess.Popen(
        ["systemctl", "--user", "restart", "rcpilot.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def force_self_upgrade() -> None:
    """Upgrade rcpilot to latest PyPI version and restart the service."""
    def _do() -> None:
        if _run_upgrade():
            _restart_service()

    threading.Thread(target=_do, daemon=True, name="self-updater-manual").start()


def _self_updater_loop(
    config: "Config", current_version: str, stop_event: threading.Event
) -> None:
    while True:
        available = _check_for_update(current_version)
        if available:
            latest = get_self_updater_state()["latest_version"]
            mode = getattr(config, "rcpilot_update_mode", "prompt")
            if mode == "auto":
                logger.info("self_updater: auto-upgrading {} → {}", current_version, latest)
                force_self_upgrade()
                return
            else:
                logger.info(
                    "self_updater: update available {} → {} (prompt mode)",
                    current_version,
                    latest,
                )

        if stop_event.wait(timeout=_CHECK_INTERVAL):
            break

    logger.info("self_updater: stopped")


def start_self_updater(
    config: "Config", current_version: str
) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_self_updater_loop,
        args=(config, current_version, stop_event),
        daemon=True,
        name="pilot-self-updater",
    )
    thread.start()
    return thread, stop_event
