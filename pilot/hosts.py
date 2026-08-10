"""
Host abstraction — run commands locally or on a configured remote host over ssh.

rcpilot manages projects on the machine it runs on plus any hosts listed as
``[[hosts]]`` in config.toml. A project is addressed by a *key*:

    "rcpilot"            → project 'rcpilot' on the local host
    "stardust:rcpilot"   → project 'rcpilot' on the host named 'stardust'

Keeping the host in the key means every existing route (``/api/sessions/{project}``)
and every DB row (``sessions.project``) works unchanged for local projects — remote
ones simply carry a prefix.

Remote execution notes:
  - ssh connections are multiplexed (ControlMaster) so the many short-lived calls
    the UI makes cost ~10ms each instead of a full handshake.
  - Shell snippets are piped to ``bash -ls`` over stdin rather than passed as an
    argument, which avoids a whole layer of quoting. A login shell is used so
    ``claude`` on ~/.local/bin resolves.
  - Login shells print MOTDs and profile noise, so every snippet emits a RS
    (0x1e) marker before its real output and callers keep only what follows.
"""

from __future__ import annotations

import secrets
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from pilot.config import Config, RemoteHost

# Separator between host name and project name in a project key.
KEY_SEP = ":"

# Output marker — everything before it is login-shell noise.
_MARKER = "\x1e"

_CONTROL_DIR = Path.home() / ".cache" / "rcpilot" / "ssh"

_SSH_BASE = [
    "ssh",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=10",
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=300",
]


class HostUnreachable(RuntimeError):
    """Raised when a remote host could not be contacted at all."""


# ---------------------------------------------------------------------------
# Project keys
# ---------------------------------------------------------------------------

def split_key(key: str) -> tuple[str, str]:
    """Split a project key into (host_name, project_name).

    Local projects have an empty host name.
    """
    if KEY_SEP in key:
        host, _, name = key.partition(KEY_SEP)
        return host, name
    return "", key


def make_key(host_name: str, project_name: str) -> str:
    """Build a project key. An empty *host_name* means the local host."""
    return f"{host_name}{KEY_SEP}{project_name}" if host_name else project_name


def resolve(config: "Config", key: str) -> tuple["RemoteHost | None", str]:
    """Resolve a project key to (host_or_None, bare_project_name).

    Returns ``(None, name)`` for local projects. Raises KeyError if the key
    names a host that is not configured.
    """
    host_name, project_name = split_key(key)
    if not host_name:
        return None, project_name
    for host in config.hosts:
        if host.name == host_name:
            return host, project_name
    raise KeyError(host_name)


# ---------------------------------------------------------------------------
# Shell quoting
# ---------------------------------------------------------------------------

def sh_path(path: str) -> str:
    """Quote a path for a remote shell, keeping a leading ``~`` expandable.

    ``shlex.quote('~/projects')`` yields ``'~/projects'``, which bash treats as
    a literal directory named '~'. Quoting only the part after the tilde keeps
    expansion working while still protecting spaces.
    """
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def _strip_noise(stdout: str) -> str:
    """Drop login-shell banner output preceding the marker."""
    return stdout.split(_MARKER, 1)[1] if _MARKER in stdout else stdout


def _ssh_argv(host: "RemoteHost") -> list[str]:
    _CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    return [
        *_SSH_BASE,
        "-o", f"ControlPath={_CONTROL_DIR}/cm-%C",
        host.ssh,
        "bash", "-ls",
    ]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_script(
    host: "RemoteHost | None",
    script: str,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    """Run a bash *script* on *host* (or locally when host is None).

    The script is fed to bash on stdin. ``stdout`` on the returned object has
    login-shell noise stripped. A marker is prepended automatically, so scripts
    should just print their payload.
    """
    body = f"printf '{_MARKER}'\n{script}"
    if host is None:
        argv = ["bash", "-lc", body]
    else:
        argv = _ssh_argv(host)
    try:
        proc = subprocess.run(
            argv,
            input="" if host is None else body,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise HostUnreachable(f"timed out running script on {host.name if host else 'local'}")
    proc = subprocess.CompletedProcess(
        proc.args, proc.returncode, _strip_noise(proc.stdout), proc.stderr
    )
    if host is not None and proc.returncode == 255:
        raise HostUnreachable(f"ssh to {host.name} ({host.ssh}) failed: {proc.stderr.strip()}")
    return proc


def run(
    host: "RemoteHost | None",
    argv: list[str],
    cwd: str | None = None,
    timeout: float = 20.0,
    env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *argv* in *cwd* on *host* (or locally when host is None).

    Local calls go straight to subprocess; remote calls are wrapped in a login
    shell. ``env`` only applies locally — a remote command inherits the login
    shell's environment (notably, no ANTHROPIC_BASE_URL, since the usage proxy
    listens on rcpilot's own loopback).
    """
    if host is None:
        return subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
        )
    command = shlex.join(argv)
    script = f"cd {sh_path(cwd)} || exit 1\n{command}\n" if cwd else f"{command}\n"
    return run_script(host, script, timeout=timeout)


def detach(
    host: "RemoteHost | None",
    argv: list[str],
    cwd: str | None,
    env: dict | None = None,
) -> None:
    """Fire-and-forget *argv* — return immediately, let it run to completion.

    Used for background work (PR reviews) that takes minutes and reports its
    result elsewhere. Locally this is a detached child process; remotely it is a
    transient systemd unit, which is what lets it outlive the ssh connection.
    """
    if host is None:
        subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        return
    inner = shlex.join(argv)
    if cwd:
        inner = f"cd {sh_path(cwd)} && exec {inner}"
    unit = f"rcpilot-task-{secrets.token_hex(4)}"
    run_script(
        host,
        f"systemd-run --user --collect --unit={unit} bash -lc {shlex.quote(inner)} "
        f">/dev/null 2>&1 || true\n",
        timeout=20.0,
    )


def accept_claude_trust(host: "RemoteHost | None", project_path: str) -> None:
    """Set hasTrustDialogAccepted for *project_path* in the host's ~/.claude.json.

    Claude Code otherwise blocks a remote-control session on a "Do you trust this
    folder?" prompt that nobody is there to answer. Remote hosts get the same
    edit applied through a python3 snippet over ssh.
    """
    if host is None:
        _accept_trust_local(Path(project_path))
        return
    script = f"""python3 - {sh_path(project_path)} <<'RCPILOT_PY'
import json, os, sys
path = os.path.expanduser('~/.claude.json')
try:
    data = json.load(open(path))
except Exception:
    data = {{}}
entry = data.setdefault('projects', {{}}).setdefault(sys.argv[1], {{}})
if not entry.get('hasTrustDialogAccepted'):
    entry['hasTrustDialogAccepted'] = True
    with open(path, 'w') as fh:
        json.dump(data, fh, indent=2)
RCPILOT_PY
"""
    try:
        proc = run_script(host, script, timeout=15.0)
        if proc.returncode != 0:
            logger.warning(
                "could not accept claude trust for {} on {}: {}",
                project_path, host.name, proc.stderr.strip(),
            )
    except HostUnreachable as exc:
        logger.warning("could not accept claude trust on {}: {}", host.name, exc)


def _accept_trust_local(project_path: Path) -> None:
    import json

    claude_json = Path.home() / ".claude.json"
    try:
        data: dict = json.loads(claude_json.read_text()) if claude_json.exists() else {}
    except Exception:
        data = {}

    projects: dict = data.setdefault("projects", {})
    key = str(project_path.resolve())
    entry: dict = projects.setdefault(key, {})
    if not entry.get("hasTrustDialogAccepted"):
        entry["hasTrustDialogAccepted"] = True
        try:
            claude_json.write_text(json.dumps(data, indent=2))
            logger.info("accepted claude trust for {}", key)
        except Exception as exc:
            logger.warning("could not write trust entry to ~/.claude.json: {}", exc)


def check_online(host: "RemoteHost") -> tuple[bool, str]:
    """Probe *host*. Returns (online, error_message)."""
    try:
        proc = run_script(host, "printf ok\n", timeout=10.0)
    except HostUnreachable as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or f"exit {proc.returncode}"
    return "ok" in proc.stdout, proc.stderr.strip()
