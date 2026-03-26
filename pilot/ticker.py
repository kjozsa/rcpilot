"""
Window ticker — fires `claude -p "hi"` at configured times to start the
5-hour rolling usage window. The clock starts on the first prompt, so a
lightweight no-op prompt is enough.

Each configured time (HH:MM, local) is fired at most once per calendar day.
The background thread checks every 30 seconds and fires within a ±30-second
window of the target minute.
"""

from __future__ import annotations

import subprocess
import threading
from datetime import date, datetime
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from pilot.config import Config

POLL_INTERVAL: float = 30.0  # seconds between checks

# Shared state — read by the /api/ticker endpoint
_state: dict = {
    "window_starts": [],
    "last_fired": None,   # "HH:MM" of most recent fire
    "last_fired_at": None,  # ISO datetime string
    "next_window": None,  # "HH:MM" of next upcoming window today (or tomorrow)
}
_state_lock = threading.Lock()


def get_ticker_state() -> dict:
    with _state_lock:
        return dict(_state)



def _parse_times(window_starts: list[str]) -> list[tuple[int, int]]:
    """Parse ["07:00", "12:00"] → [(7, 0), (12, 0)]."""
    result = []
    for t in window_starts:
        try:
            h, m = t.strip().split(":")
            result.append((int(h), int(m)))
        except Exception:
            logger.warning("ticker: invalid window_starts entry {!r}, skipping", t)
    return sorted(result)


def _next_window(times: list[tuple[int, int]], now: datetime) -> str | None:
    """Return HH:MM of the next upcoming window from *now*, or None if empty."""
    for h, m in times:
        if (h, m) > (now.hour, now.minute):
            return f"{h:02d}:{m:02d}"
    # All windows passed today — next is first window tomorrow
    if times:
        h, m = times[0]
        return f"{h:02d}:{m:02d}"
    return None


def _ticker_loop(config: "Config", stop_event: threading.Event) -> None:
    times = _parse_times(config.window_starts)
    if not times:
        logger.info("ticker: no window_starts configured, ticker idle")
        return

    logger.info("ticker: windows configured at {}", [f"{h:02d}:{m:02d}" for h, m in times])

    # Track which (date, HH:MM) combos have already fired
    fired: set[tuple[date, str]] = set()

    with _state_lock:
        _state["window_starts"] = config.window_starts[:]
        _state["next_window"] = _next_window(times, datetime.now())

    while not stop_event.wait(timeout=POLL_INTERVAL):
        now = datetime.now()
        for h, m in times:
            label = f"{h:02d}:{m:02d}"
            key = (now.date(), label)
            # Fire if within the target minute and not yet fired today
            if now.hour == h and now.minute == m and key not in fired:
                fired.add(key)
                _fire(config, label)

        with _state_lock:
            _state["next_window"] = _next_window(times, now)

    logger.info("ticker: stopped")


def _fire(config: "Config", label: str) -> None:
    logger.info("ticker: firing window tick for {}", label)
    try:
        result = subprocess.run(
            ["claude", "-p", "hi"],
            cwd=str(config.projects_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("ticker: window tick OK for {}", label)
        else:
            logger.warning("ticker: claude returned {} for {}: {}", result.returncode, label, result.stderr.strip())
    except Exception:
        logger.exception("ticker: failed to fire for {}", label)

    with _state_lock:
        _state["last_fired"] = label
        _state["last_fired_at"] = datetime.now().isoformat(timespec="seconds")


def start_ticker(config: "Config") -> tuple[threading.Thread, threading.Event]:
    """Start the ticker thread. Returns (thread, stop_event)."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_ticker_loop,
        args=(config, stop_event),
        daemon=True,
        name="pilot-ticker",
    )
    thread.start()
    return thread, stop_event
