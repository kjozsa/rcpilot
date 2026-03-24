"""
Config loading for pi-lot.

Reads from ~/.config/pi-lot/config.toml by default, or from the path
specified by the PILOT_CONFIG environment variable.
"""

from __future__ import annotations

import os
import tomllib  # stdlib since Python 3.11
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pi-lot" / "config.toml"


@dataclass
class Config:
    # Directory scanned for projects — each immediate subdirectory is a project
    projects_dir: Path = field(default_factory=lambda: Path.home() / "projects")
    # Uvicorn bind host; 0.0.0.0 makes it reachable over a VPN
    host: str = "0.0.0.0"
    port: int = 8000
    # Prefix prepended to every tmux session name to avoid collisions
    tmux_session_prefix: str = "pilot-"
    # Passed as --spawn=<value> to claude remote-control; skips the interactive
    # first-run prompt. Options: "session" | "same-dir" | "worktree"
    # "session" produces a /session_xxx URL that the Claude mobile app handles correctly.
    spawn_mode: str = "session"
    # SQLite database file path
    db_path: Path = field(default_factory=lambda: Path.home() / ".config" / "pi-lot" / "pilot.db")


def load_config(path: Path | None = None) -> Config:
    """
    Load config from *path* (or PILOT_CONFIG env var, or the default location).
    Missing file → return defaults so the app works out-of-the-box on first run.
    """
    if path is None:
        env_path = os.environ.get("PILOT_CONFIG")
        path = Path(env_path) if env_path else DEFAULT_CONFIG_PATH

    if not path.exists():
        return Config()

    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    # Only pull recognised keys; ignore anything unknown so old configs stay valid
    kwargs: dict = {}

    if "projects_dir" in raw:
        kwargs["projects_dir"] = Path(raw["projects_dir"]).expanduser()
    if "host" in raw:
        kwargs["host"] = str(raw["host"])
    if "port" in raw:
        kwargs["port"] = int(raw["port"])
    if "tmux_session_prefix" in raw:
        kwargs["tmux_session_prefix"] = str(raw["tmux_session_prefix"])
    if "spawn_mode" in raw:
        kwargs["spawn_mode"] = str(raw["spawn_mode"])
    if "db_path" in raw:
        kwargs["db_path"] = Path(raw["db_path"]).expanduser()

    return Config(**kwargs)
