"""
Background job that runs `claude update` on a cron schedule and tracks the
installed Claude CLI version.

On startup: reads current version via `claude --version` (no update).
On cron fire: runs `claude update`, then reads the new version.

Default schedule: twice daily at 06:00 and 18:00.
Configure in config.toml:
  claude_update_cron = "0 6,18 * * *"

Supported cron syntax (5 fields: minute hour dom month dow):
  *        any value
  */n      every n steps
  a,b,c    list of values
  a-b      inclusive range
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from pilot.config import Config

POLL_INTERVAL: float = 30.0  # seconds between cron checks
DEFAULT_CRON = "0 6,18 * * *"
CLAUDE_FALLBACK = "/home/pi/.local/bin/claude"


def _claude_bin() -> str:
    return shutil.which("claude") or CLAUDE_FALLBACK

_state: dict = {
    "claude_version": None,     # e.g. "1.2.3"
    "last_check_at": None,      # ISO datetime of last update check attempt
    "last_update_at": None,     # ISO datetime of last version change (new version installed)
    "last_update_ok": None,     # bool
}
_state_lock = threading.Lock()


def get_updater_state() -> dict:
    with _state_lock:
        return dict(_state)


# ---------------------------------------------------------------------------
# Cron helpers (same logic as ticker.py)
# ---------------------------------------------------------------------------

def _expand_field(field: str, lo: int, hi: int) -> set[int]:
    result: set[int] = set()
    for part in field.split(","):
        if part == "*":
            result.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            result.update(range(lo, hi + 1, step))
        elif "-" in part:
            a, b = part.split("-", 1)
            result.update(range(int(a), int(b) + 1))
        else:
            result.add(int(part))
    return result


def _parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(fields)!r}: {expr!r}")
    return (
        _expand_field(fields[0], 0, 59),
        _expand_field(fields[1], 0, 23),
        _expand_field(fields[2], 1, 31),
        _expand_field(fields[3], 1, 12),
        _expand_field(fields[4], 0, 6),
    )


def _matches(dt: datetime, parsed: tuple) -> bool:
    mins, hours, doms, months, dows = parsed
    return (
        dt.minute in mins
        and dt.hour in hours
        and dt.day in doms
        and dt.month in months
        and dt.weekday() % 7 in dows
    )


# ---------------------------------------------------------------------------
# Claude version helpers
# ---------------------------------------------------------------------------

def _read_claude_version() -> str | None:
    """Run `claude --version` and return the version string, or None on failure."""
    try:
        result = subprocess.run(
            [_claude_bin(), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            output = result.stdout.strip() or result.stderr.strip()
            # Output is typically "1.2.3" or "Claude Code 1.2.3"
            match = re.search(r"(\d+\.\d+\.\d+)", output)
            if match:
                return match.group(1)
            # Fallback: last whitespace-separated token
            parts = output.split()
            return parts[-1] if parts else None
    except Exception:
        logger.exception("updater: failed to read claude version")
    return None


# ---------------------------------------------------------------------------
# Updater loop
# ---------------------------------------------------------------------------

def _updater_loop(config: "Config", stop_event: threading.Event) -> None:
    expr = getattr(config, "claude_update_cron", None) or DEFAULT_CRON

    try:
        parsed = _parse_cron(expr)
    except ValueError:
        logger.error("updater: invalid claude_update_cron {!r}, using default", expr)
        parsed = _parse_cron(DEFAULT_CRON)

    logger.info("updater: cron schedule active — {!r}", expr)

    # Read current version at startup (no update)
    version = _read_claude_version()
    if version:
        with _state_lock:
            _state["claude_version"] = version
        logger.info("updater: current claude version {}", version)
    else:
        logger.warning("updater: could not read claude version at startup")

    last_fired_minute: datetime | None = None

    while not stop_event.wait(timeout=POLL_INTERVAL):
        now = datetime.now().replace(second=0, microsecond=0)
        if _matches(now, parsed) and now != last_fired_minute:
            last_fired_minute = now
            _run_update(now)

    logger.info("updater: stopped")


def _run_update(now: datetime) -> None:
    label = now.strftime("%H:%M")
    logger.info("updater: running claude update at {}", label)
    ok = False
    try:
        result = subprocess.run(
            [_claude_bin(), "update"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        ok = result.returncode == 0
        if ok:
            logger.info("updater: claude update OK at {}", label)
        else:
            logger.warning("updater: claude update exited {} at {}: {}", result.returncode, label, result.stderr.strip())
    except Exception:
        logger.exception("updater: claude update failed at {}", label)

    # Always refresh the version after an update attempt
    version = _read_claude_version()
    with _state_lock:
        old_version = _state["claude_version"]
        if version:
            _state["claude_version"] = version
        _state["last_check_at"] = now.isoformat(timespec="seconds")
        if ok and version and version != old_version:
            _state["last_update_at"] = now.isoformat(timespec="seconds")
        _state["last_update_ok"] = ok


def force_update() -> None:
    """Trigger a claude update immediately in a background thread."""
    threading.Thread(target=_run_update, args=(datetime.now(),), daemon=True, name="pilot-updater-manual").start()


def start_updater(config: "Config") -> tuple[threading.Thread, threading.Event]:
    """Start the updater thread. Returns (thread, stop_event)."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_updater_loop,
        args=(config, stop_event),
        daemon=True,
        name="pilot-updater",
    )
    thread.start()
    return thread, stop_event
