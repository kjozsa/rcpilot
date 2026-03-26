"""
Project discovery — scans projects_dir and returns lightweight metadata.
No session awareness here; keep this module pure filesystem.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TypedDict


class Project(TypedDict):
    name: str
    path: str       # absolute path as string (JSON-friendly)
    has_git: bool
    git_diff_stat: str | None   # output of `git diff --shortstat`, or None
    git_branch: str | None      # current branch name, or None


def _git_diff_stat(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "diff", "--shortstat"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _git_branch(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def list_projects(projects_dir: Path, sort_by: str = "modified") -> list[Project]:
    """
    Return one Project entry for every immediate subdirectory of *projects_dir*.

    Directories whose names start with '.' are silently skipped — they're
    typically tool-managed (e.g. .venv accidentally placed at the root).
    
    Args:
        projects_dir: Directory containing project subdirectories
        sort_by: Sort order - "modified" (most recent first) or "alpha" (A-Z)
    """
    if not projects_dir.exists():
        return []

    results: list[Project] = []
    for entry in projects_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue

        has_git = (entry / ".git").exists()
        results.append(
            Project(
                name=entry.name,
                path=str(entry.resolve()),
                has_git=has_git,
                git_diff_stat=_git_diff_stat(entry) if has_git else None,
                git_branch=_git_branch(entry) if has_git else None,
            )
        )

    # Sort based on preference
    if sort_by == "alpha":
        results.sort(key=lambda p: p["name"].lower())
    else:  # "modified" or default
        results.sort(key=lambda p: Path(p["path"]).stat().st_mtime, reverse=True)
    
    return results
