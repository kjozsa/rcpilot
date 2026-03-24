"""
Project discovery — scans projects_dir and returns lightweight metadata.
No tmux / session awareness here; keep this module pure filesystem.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict


class Project(TypedDict):
    name: str
    path: str       # absolute path as string (JSON-friendly)
    has_git: bool


def list_projects(projects_dir: Path) -> list[Project]:
    """
    Return one Project entry for every immediate subdirectory of *projects_dir*.

    Directories whose names start with '.' are silently skipped — they're
    typically tool-managed (e.g. .venv accidentally placed at the root).
    """
    if not projects_dir.exists():
        return []

    results: list[Project] = []
    for entry in sorted(projects_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue

        results.append(
            Project(
                name=entry.name,
                path=str(entry.resolve()),
                has_git=(entry / ".git").exists(),
            )
        )

    return results
