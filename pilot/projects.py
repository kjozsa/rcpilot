"""
Project discovery — scans projects_dir and returns lightweight metadata.
No session awareness here; keep this module pure filesystem (plus ssh for the
remote hosts, which is the same scan run on the far end).
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from loguru import logger

from pilot import hosts as host_mgr

if TYPE_CHECKING:
    from pilot.config import Config, RemoteHost


class Project(TypedDict):
    name: str       # project *key* — 'rcpilot' locally, 'stardust:rcpilot' remotely
    label: str      # bare directory name, for display
    host: str       # host name, or '' for the local machine
    path: str       # absolute path on its own host (JSON-friendly)
    has_git: bool
    git_diff_stat: str | None   # output of `git diff --shortstat`, or None
    git_branch: str | None      # current branch name, or None
    git_hash: str | None        # short commit hash of HEAD, or None
    git_commit_time: str | None # ISO timestamp of HEAD commit, or None
    mtime: float                # directory mtime, used for "recent" sorting


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


def _git_head_info(path: Path) -> tuple[str | None, str | None]:
    """Return (short_hash, iso_timestamp) for HEAD, or (None, None) on failure."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h\t%cI"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("\t", 1)
            return parts[0], parts[1] if len(parts) > 1 else None
    except Exception:
        pass
    return None, None


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
        git_hash, git_commit_time = _git_head_info(entry) if has_git else (None, None)
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = 0.0
        results.append(
            Project(
                name=entry.name,
                label=entry.name,
                host="",
                path=str(entry.resolve()),
                has_git=has_git,
                git_diff_stat=_git_diff_stat(entry) if has_git else None,
                git_branch=_git_branch(entry) if has_git else None,
                git_hash=git_hash,
                git_commit_time=git_commit_time,
                mtime=mtime,
            )
        )

    return sort_projects(results, sort_by)


def sort_projects(items: list[Project], sort_by: str) -> list[Project]:
    """Sort projects alphabetically or by directory mtime (most recent first)."""
    if sort_by == "alpha":
        return sorted(items, key=lambda p: p["label"].lower())
    return sorted(items, key=lambda p: p.get("mtime") or 0.0, reverse=True)


# ---------------------------------------------------------------------------
# Remote hosts
# ---------------------------------------------------------------------------

# One pass over each remote projects dir, emitting a tab-separated row per repo.
# Doing it in a single shell script keeps a host scan to one ssh round trip
# instead of four git calls per project.
_REMOTE_SCAN = r"""
scan() (
  cd "$1" 2>/dev/null || return 0
  for d in */; do
    d=${d%/}
    case "$d" in .*) continue;; esac
    [ -d "$d" ] || continue
    branch=''; hash=''; ctime=''; stat_out=''; git=0
    if [ -e "$d/.git" ]; then
      git=1
      branch=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null)
      hash=$(git -C "$d" log -1 --format=%h 2>/dev/null)
      ctime=$(git -C "$d" log -1 --format=%cI 2>/dev/null)
      stat_out=$(git -C "$d" diff --shortstat 2>/dev/null)
    fi
    mtime=$(stat -c %Y "$d" 2>/dev/null || echo 0)
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$d" "$PWD/$d" "$git" "$branch" "$hash" "$ctime" "$stat_out" "$mtime"
  done
)
"""

# Remote scans are re-run at most this often; the UI reloads the project list
# after most actions, and a few seconds of staleness is invisible.
_SCAN_TTL = 15.0
_scan_cache: dict[str, tuple[float, list[Project]]] = {}
# One lock per host. Loading the page fires a /api/sessions request per project,
# and each one resolves its project against the host scan — with 57 remote
# projects that meant dozens of threads racing to run the same ssh scan the
# moment the cache expired, and every request paying for it. The lock makes the
# first caller do the work while the rest wait and then read its result.
_scan_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _host_lock(name: str) -> threading.Lock:
    with _locks_guard:
        return _scan_locks.setdefault(name, threading.Lock())


def _cached_scan(host_name: str, use_cache: bool) -> list[Project] | None:
    entry = _scan_cache.get(host_name)
    if use_cache and entry and time.monotonic() - entry[0] < _SCAN_TTL:
        return entry[1]
    return None


def list_projects_on_host(host: "RemoteHost", use_cache: bool = True) -> list[Project]:
    """Return the projects on *host*. Raises HostUnreachable if ssh fails."""
    hit = _cached_scan(host.name, use_cache)
    if hit is not None:
        return hit
    with _host_lock(host.name):
        # Another thread may have refreshed the cache while we waited.
        hit = _cached_scan(host.name, use_cache)
        if hit is not None:
            return hit
        return _scan_host(host)


def _scan_host(host: "RemoteHost") -> list[Project]:
    script = _REMOTE_SCAN + "".join(
        f"scan {host_mgr.sh_path(d)}\n" for d in host.projects_dirs
    )
    proc = host_mgr.run_script(host, script, timeout=25.0)
    if proc.returncode != 0:
        raise host_mgr.HostUnreachable(
            f"scanning {', '.join(host.projects_dirs)} on {host.name} failed: "
            f"{proc.stderr.strip() or f'exit {proc.returncode}'}"
        )

    results: list[Project] = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 8:
            continue
        label, path, git, branch, git_hash, ctime, diff_stat, mtime = fields
        if label in seen:
            # Two scanned roots hold a directory of the same name; the key can
            # only point at one of them, so the earlier root wins.
            logger.warning(
                "host {}: duplicate project name {!r} at {} — keeping the first",
                host.name, label, path,
            )
            continue
        seen.add(label)
        results.append(
            Project(
                name=host_mgr.make_key(host.name, label),
                label=label,
                host=host.name,
                path=path,
                has_git=git == "1",
                git_diff_stat=diff_stat or None,
                git_branch=branch or None,
                git_hash=git_hash or None,
                git_commit_time=ctime or None,
                mtime=float(mtime or 0),
            )
        )
    _scan_cache[host.name] = (time.monotonic(), results)
    return results


def invalidate_host_cache(host_name: str = "") -> None:
    """Drop cached scan results so the next list reflects a just-made change."""
    if host_name:
        _scan_cache.pop(host_name, None)
    else:
        _scan_cache.clear()


def list_all_projects(config: "Config", sort_by: str = "modified") -> list[Project]:
    """Local projects plus those on every configured host, as one sorted list.

    A host that cannot be reached contributes nothing rather than raising — the
    local projects (and any other host) stay usable. /api/hosts reports the
    failure so the UI can show it.
    """
    results = list(list_projects(config.projects_dir, sort_by=sort_by))
    for host in config.hosts:
        try:
            results.extend(list_projects_on_host(host))
        except host_mgr.HostUnreachable as exc:
            logger.warning("host {} unreachable: {}", host.name, exc)
    return sort_projects(results, sort_by)
