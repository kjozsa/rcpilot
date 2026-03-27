"""
Cron-based scheduler — fires `claude -p "hi"` on a cron schedule to start
the 5-hour rolling usage window. The clock starts on the first prompt, so a
lightweight no-op prompt is enough.

Configure in config.toml:
  window_cron = "0 7,12 * * *"   # fire at 07:00 and 12:00 every day

Supported cron syntax (5 fields: minute hour dom month dow):
  *        any value
  */n      every n steps
  a,b,c    list of values
  a-b      inclusive range
"""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from pilot.config import Config

POLL_INTERVAL: float = 30.0  # seconds between checks

_state: dict = {
    "window_cron": None,
    "last_fired_at": None,   # ISO datetime string of most recent fire
    "next_fire": None,       # ISO datetime string of next scheduled fire
    "reset_at": None,        # ISO datetime string when usage resets (last_fired_at + 5h)
    "fire_log": [],          # last 5 fires: [{fired_at, ok, detail}]
}
_state_lock = threading.Lock()


def get_ticker_state() -> dict:
    with _state_lock:
        return dict(_state)


# ---------------------------------------------------------------------------
# Minimal cron field parser — no dependencies
# ---------------------------------------------------------------------------

def _expand_field(field: str, lo: int, hi: int) -> set[int]:
    """Expand a single cron field string into a set of matching integers."""
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
    """Parse a 5-field cron expression into (minutes, hours, doms, months, dows)."""
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(fields)!r}: {expr!r}")
    mins   = _expand_field(fields[0], 0, 59)
    hours  = _expand_field(fields[1], 0, 23)
    doms   = _expand_field(fields[2], 1, 31)
    months = _expand_field(fields[3], 1, 12)
    dows   = _expand_field(fields[4], 0, 6)   # 0 = Sunday
    return mins, hours, doms, months, dows


def _matches(dt: datetime, parsed: tuple) -> bool:
    mins, hours, doms, months, dows = parsed
    return (
        dt.minute in mins
        and dt.hour in hours
        and dt.day in doms
        and dt.month in months
        and dt.weekday() % 7 in dows  # Python Mon=0; cron Sun=0 → shift
    )


def _next_fire(parsed: tuple, after: datetime) -> datetime:
    """Return the next datetime (minute precision) matching *parsed* after *after*."""
    candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(60 * 24 * 366):  # search up to ~1 year
        if _matches(candidate, parsed):
            return candidate
        candidate += timedelta(minutes=1)
    raise RuntimeError("no matching cron time found within 1 year")


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

def _ticker_loop(config: "Config", stop_event: threading.Event) -> None:
    expr = config.window_cron
    if not expr:
        logger.info("ticker: no window_cron configured, scheduler idle")
        return

    try:
        parsed = _parse_cron(expr)
    except ValueError:
        logger.error("ticker: invalid window_cron {!r}, scheduler disabled", expr)
        return

    logger.info("ticker: cron scheduler active — {!r}", expr)

    # Track which minute we last fired so we don't double-fire
    last_fired_minute: datetime | None = None

    with _state_lock:
        _state["window_cron"] = expr
        _state["next_fire"] = _next_fire(parsed, datetime.now()).isoformat(timespec="minutes")

    while not stop_event.wait(timeout=POLL_INTERVAL):
        now = datetime.now().replace(second=0, microsecond=0)
        if _matches(now, parsed) and now != last_fired_minute:
            last_fired_minute = now
            _fire(config, now)
        with _state_lock:
            _state["next_fire"] = _next_fire(parsed, datetime.now()).isoformat(timespec="minutes")

    logger.info("ticker: stopped")


def _fire(config: "Config", now: datetime) -> None:
    import os
    label = now.strftime("%H:%M")
    logger.info("ticker: firing cron job at {}", label)
    ok = False
    detail = ""
    try:
        env = os.environ.copy()
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{config.port}/proxy"
        result = subprocess.run(
            ["claude", "-p", "hi"],
            cwd=str(config.projects_dir),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        ok = result.returncode == 0
        detail = result.stderr.strip() if not ok else ""
        if ok:
            logger.info("ticker: cron fire OK at {}", label)
        else:
            logger.warning("ticker: claude exited {} at {}: {}", result.returncode, label, detail)
    except Exception as e:
        detail = str(e)
        logger.exception("ticker: cron fire failed at {}", label)

    entry = {"fired_at": now.isoformat(timespec="seconds"), "ok": ok, "detail": detail}
    reset_time = now + timedelta(hours=5)
    with _state_lock:
        _state["last_fired_at"] = now.isoformat(timespec="seconds")
        _state["reset_at"] = reset_time.isoformat(timespec="seconds")
        _state["fire_log"] = ([entry] + _state["fire_log"])[:5]


def start_ticker(config: "Config") -> tuple[threading.Thread, threading.Event]:
    """Start the scheduler thread. Returns (thread, stop_event)."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_ticker_loop,
        args=(config, stop_event),
        daemon=True,
        name="pilot-ticker",
    )
    thread.start()
    return thread, stop_event
