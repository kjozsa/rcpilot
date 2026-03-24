"""
Phase 1 data shapes — defined here so imports compile and the structure is
established. No persistence logic yet; that lives in db.py (Phase 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class SessionRecord:
    """One row in the ``sessions`` SQLite table."""

    id: int
    project: str              # matches Project.name
    name: Optional[str]       # auto-set to ISO date at creation (e.g. "2026-03-24")
    started_at: datetime
    ended_at: Optional[datetime]
    # One of: "running" | "stopped" | "timed_out" | "error"
    status: str
    rc_url: Optional[str]     # the Remote-Control URL captured from claude stdout
