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
# See https://github.com/kjozsa/rcpilot for full documentation.

# Directory scanned for projects — each immediate subdirectory is a project.
projects_dir = "{projects_dir}"

# Uvicorn bind host; 0.0.0.0 makes it reachable over a VPN.
host = "0.0.0.0"
port = {port}

# SQLite database file path.
db_path = "~/.config/rcpilot/pilot.db"

# Claude usage window scheduler — fires "claude -p hi" on a cron schedule to
# start the 5-hour rolling usage window (Pro/Max plans). Standard 5-field cron.
# Example: window_cron = "0 7,12,17 * * *"   # fire at 07:00, 12:00, and 17:00 daily
window_cron = "0 7,12,17 * * *"

# Permission mode for newly spawned sessions. One of: default, auto, acceptEdits,
# dontAsk, plan, bypassPermissions. YOLO mode overrides this with bypassPermissions.
permission_mode = "auto"

# ── Security (optional) ────────────────────────────────────────────────────
# Protect the UI with a keyphrase. Without this, anyone on your network can
# access rcpilot. Recommended if visitors use your local network.
# admin_keyphrase = "your-secret-here"

# Enable HTTPS. Without TLS the keyphrase is visible on the network in plain
# text, so set both together. Generate a trusted local cert with mkcert:
#   mkcert -install   # once per machine/browser
#   mkdir -p ~/.config/rcpilot/tls
#   mkcert -key-file ~/.config/rcpilot/tls/key.pem \\
#          -cert-file ~/.config/rcpilot/tls/cert.pem \\
#          localhost <hostname> <ip>
# ssl_certfile = "~/.config/rcpilot/tls/cert.pem"
# ssl_keyfile  = "~/.config/rcpilot/tls/key.pem"

# ── Remote hosts (optional) ────────────────────────────────────────────────
# Manage projects on other machines over ssh. Each host needs passwordless ssh
# (key-based) from this machine, claude on its PATH, and a systemd user manager
# that survives logout:  loginctl enable-linger $USER
#
# [[hosts]]
# name = "stardust"           # label shown in the UI; also prefixes project keys
# ssh = "kjozsa@stardust"     # anything ssh accepts, incl. ~/.ssh/config aliases
# projects_dir = "~/projects" # path on that machine, or a list of paths
"""


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "rcpilot" / "config.toml"


@dataclass(frozen=True)
class RemoteHost:
    """A machine reachable over ssh whose projects rcpilot also manages."""
    # Label shown in the UI; also the prefix in project keys ("stardust:rcpilot")
    name: str
    # ssh destination — user@host, or an alias from ~/.ssh/config
    ssh: str
    # Directories scanned *on that machine*, in order. Kept as strings so ~
    # expands over there, not here. The first one is where new projects land.
    projects_dirs: tuple[str, ...] = ("~/projects",)

    @property
    def projects_dir(self) -> str:
        """Default directory for newly created or cloned projects."""
        return self.projects_dirs[0]


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
    # Cron expression for claude auto-update (default: 06:00 and 18:00 daily)
    claude_update_cron: str = "0 6,18 * * *"
    # Optional admin keyphrase — if set, UI requires login
    admin_keyphrase: str = ""
    # Optional TLS certificate and key paths for HTTPS
    ssl_certfile: str = ""
    ssl_keyfile: str = ""
    # HTTP-only proxy port for localhost (used as ANTHROPIC_BASE_URL when TLS is enabled).
    # 0 = auto (port + 1). Only needed when ssl_certfile/ssl_keyfile are set.
    proxy_port: int = 0
    # Self-update mode: "prompt" shows a banner when a new version is available;
    # "auto" upgrades and restarts silently.
    rcpilot_update_mode: str = "prompt"
    # Permission mode for newly spawned sessions. One of: default, auto,
    # acceptEdits, dontAsk, plan, bypassPermissions. YOLO mode overrides this
    # with bypassPermissions.
    permission_mode: str = "auto"
    # Remote hosts whose projects are managed alongside the local ones
    hosts: list[RemoteHost] = field(default_factory=list)


def _prompt_first_run() -> tuple[str, int]:
    defaults = ("~/projects", 8000)
    try:
        projects = input(f"Projects directory [{defaults[0]}]: ").strip()
        port_str = input(f"Port [{defaults[1]}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return defaults
    projects = projects or defaults[0]
    try:
        port = int(port_str) if port_str else defaults[1]
    except ValueError:
        port = defaults[1]
    return projects, port


def load_config(path: Path | None = None) -> Config:
    """
    Load config from *path* (or PILOT_CONFIG env var, or the default location).
    Missing file → return defaults so the app works out-of-the-box on first run.
    """
    if path is None:
        env_path = os.environ.get("PILOT_CONFIG")
        path = Path(env_path) if env_path else DEFAULT_CONFIG_PATH

    if not path.exists():
        projects_dir, port = _prompt_first_run()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_CONFIG_TEMPLATE.format(projects_dir=projects_dir, port=port))
        from loguru import logger
        logger.info("created config at {} with projects_dir={} port={}", path, projects_dir, port)
        return Config(projects_dir=Path(projects_dir).expanduser(), port=port)

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
    if "claude_update_cron" in raw:
        kwargs["claude_update_cron"] = str(raw["claude_update_cron"]).strip()
    if "admin_keyphrase" in raw:
        kwargs["admin_keyphrase"] = str(raw["admin_keyphrase"]).strip()
    if "ssl_certfile" in raw:
        kwargs["ssl_certfile"] = str(Path(raw["ssl_certfile"]).expanduser())
    if "ssl_keyfile" in raw:
        kwargs["ssl_keyfile"] = str(Path(raw["ssl_keyfile"]).expanduser())
    if "proxy_port" in raw:
        kwargs["proxy_port"] = int(raw["proxy_port"])
    if "rcpilot_update_mode" in raw:
        kwargs["rcpilot_update_mode"] = str(raw["rcpilot_update_mode"]).strip()
    if "permission_mode" in raw:
        kwargs["permission_mode"] = str(raw["permission_mode"]).strip()
    if "hosts" in raw:
        kwargs["hosts"] = _parse_hosts(raw["hosts"])

    return Config(**kwargs)


def _parse_hosts(raw_hosts: object) -> list[RemoteHost]:
    """Build RemoteHost entries from the ``[[hosts]]`` config array.

    Malformed entries are skipped with a warning rather than failing startup —
    a typo in one host should not take rcpilot down for every other project.
    """
    from loguru import logger

    hosts: list[RemoteHost] = []
    if not isinstance(raw_hosts, list):
        logger.warning("config: 'hosts' must be an array of tables — ignoring")
        return hosts
    seen: set[str] = set()
    for entry in raw_hosts:
        if not isinstance(entry, dict):
            logger.warning("config: ignoring non-table entry in 'hosts'")
            continue
        name = str(entry.get("name", "")).strip()
        ssh = str(entry.get("ssh", "")).strip()
        if not name or not ssh:
            logger.warning("config: host entry needs both 'name' and 'ssh' — ignoring {}", entry)
            continue
        # ':' separates host from project in a project key, and '/' would break
        # the API path segment those keys travel in.
        if ":" in name or "/" in name:
            logger.warning("config: host name {!r} may not contain ':' or '/' — ignoring", name)
            continue
        if name in seen:
            logger.warning("config: duplicate host name {!r} — ignoring", name)
            continue
        seen.add(name)
        # projects_dir accepts a single path or a list of them — a machine that
        # keeps its repos under several roots shouldn't need duplicate entries.
        raw_dirs = entry.get("projects_dir", entry.get("projects_dirs", "~/projects"))
        if isinstance(raw_dirs, str):
            raw_dirs = [raw_dirs]
        dirs = tuple(str(d).strip() for d in raw_dirs if str(d).strip())
        if not dirs:
            logger.warning("config: host {!r} has no usable projects_dir — ignoring", name)
            continue
        hosts.append(RemoteHost(name=name, ssh=ssh, projects_dirs=dirs))
    return hosts
