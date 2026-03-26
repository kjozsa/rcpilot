"""
Config loading for rcpilot.

Reads from ~/.config/rcpilot/config.toml by default, or from the path
specified by the PILOT_CONFIG environment variable.
"""

from __future__ import annotations

import os
import tomllib  # stdlib since Python 3.11
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_CONFIG_TEMPLATE = """\
# rcpilot configuration
# See https://github.com/your-org/rcpilot for full documentation.

# Directory scanned for projects — each immediate subdirectory is a project.
projects_dir = "~/projects"

# Uvicorn bind host; 0.0.0.0 makes it reachable over a VPN.
host = "0.0.0.0"
port = 8000

# SQLite database file path.
db_path = "~/.config/rcpilot/pilot.db"

# Claude usage window ticker — fires "claude -p hi" at each listed time to start
# the 5-hour rolling usage window. Times are local HH:MM (24-hour).
# Example: window_starts = ["07:00", "12:00"]
window_starts = []
"""


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "rcpilot" / "config.toml"


@dataclass
class Config:
    # Directory scanned for projects — each immediate subdirectory is a project
    projects_dir: Path = field(default_factory=lambda: Path.home() / "projects")
    # Uvicorn bind host; 0.0.0.0 makes it reachable over a VPN
    host: str = "0.0.0.0"
    port: int = 8000
    # SQLite database file path
    db_path: Path = field(default_factory=lambda: Path.home() / ".config" / "rcpilot" / "pilot.db")
    # Window ticker: list of "HH:MM" times to fire claude -p to start usage window
    window_starts: list = field(default_factory=list)


def load_config(path: Path | None = None) -> Config:
    """
    Load config from *path* (or PILOT_CONFIG env var, or the default location).
    Missing file → return defaults so the app works out-of-the-box on first run.
    """
    if path is None:
        env_path = os.environ.get("PILOT_CONFIG")
        path = Path(env_path) if env_path else DEFAULT_CONFIG_PATH

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_CONFIG_TEMPLATE)
        from loguru import logger
        logger.info("created default config at {}", path)
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
    if "db_path" in raw:
        kwargs["db_path"] = Path(raw["db_path"]).expanduser()
    if "window_starts" in raw:
        kwargs["window_starts"] = [str(t) for t in raw["window_starts"]]

    return Config(**kwargs)
