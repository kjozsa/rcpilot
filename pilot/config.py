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

# Claude usage window scheduler — fires "claude -p hi" on a cron schedule to
# start the 5-hour rolling usage window (Pro/Max plans). Standard 5-field cron.
# Example: window_cron = "0 7,12 * * *"   # fire at 07:00 and 12:00 daily
window_cron = ""
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
    # Cron expression for usage window scheduler (empty = disabled)
    window_cron: str = ""


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
    if "window_cron" in raw:
        kwargs["window_cron"] = str(raw["window_cron"]).strip()

    return Config(**kwargs)
