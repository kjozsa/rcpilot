"""
Session management — spawn / kill / query claude remote-control via `script`.

Each session runs `claude remote-control --spawn=same-dir` inside the `script`
command, which creates and owns a PTY independently of this Python process.
All output is written to a log file. Because `script` is a separate OS process,
sessions survive FastAPI restarts — the log file and pid are persisted in SQLite.

Flow (local host):
  start  → spawn `script -q -e -c "claude ..." {log_path}` detached
           → poll log file until RC URL appears
           → store pid + log_path + url in DB
  list   → check os.kill(pid, 0) for each running DB record
  kill   → SIGTERM the script process tree (script + claude + its sessions),
             wait for it to exit (SIGKILL after a grace period)
           → read log file for snapshot

Sessions on a remote host follow the same shape with systemd standing in for the
process group: `script` runs inside a transient `systemd-run --user` unit, which
is what detaches it from the ssh connection that started it. Liveness is
`systemctl is-active`, kill is `systemctl stop`, and the log lives on that host —
so listing and killing cost an ssh round trip rather than a syscall. A host we
cannot reach yields *unknown*, never *dead*: a flaky network must not garbage
collect live sessions.
"""

from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

import pilot.db as db
from pilot import hosts as host_mgr

if TYPE_CHECKING:
    from pilot.config import RemoteHost

# `claude remote-control` prints two different URLs, and the difference is the
# whole ballgame for the attach button:
#
#   https://claude.ai/code?environment=env_…   the *environment*. Opening it
#       hands you a brand-new on-demand session in that directory — not the
#       conversation you were having. Open it twice, get two new sessions.
#   https://claude.ai/code/session_…?from=cli  the *session* deep link, emitted
#       as an OSC-8 hyperlink once the pre-created session registers (a few
#       seconds after the environment URL). This one resumes that conversation.
#
# Attach must use the session link. The environment URL is kept only as a
# fallback for the case where the session link never shows up.
_RC_SESSION_URL_PATTERN = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]+")
_RC_ENV_URL_PATTERN = re.compile(r"https://claude\.ai/code\?environment=[A-Za-z0-9_=&%-]+")

# Strip ANSI escape codes (present in PTY output captured by `script`)
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Seconds to wait for the RC URL to appear in the log file. The session deep
# link lands roughly 7s after the environment URL, so this is mostly headroom.
_URL_WAIT_SECONDS = 60
_POLL_INTERVAL = 0.3

# Seconds a killed session gets to exit after SIGTERM before it is SIGKILLed.
# Waiting matters: claude allows one bridge per folder, so a respawn that races
# the old bridge's exit dies with "This folder is already served".
_KILL_GRACE_SECONDS = 10.0

# What claude prints when another bridge already owns the folder. It then lingers
# for about a minute ("Exiting in about 60 seconds.") instead of exiting, so the
# message itself — not the process exit — is what ends the wait.
_ALREADY_SERVED = "already served by a terminal `claude remote-control`"


def _extract_urls(text: str) -> tuple[str | None, str | None]:
    """Pull the (session, environment) URLs out of captured terminal output.

    The *last* session link wins: if extra on-demand sessions were created in
    this environment, the newest is the one currently live.
    """
    sessions = _RC_SESSION_URL_PATTERN.findall(text)
    env = _RC_ENV_URL_PATTERN.search(text)
    return (sessions[-1] if sessions else None), (env.group(0) if env else None)


def _pid_alive(pid: int) -> bool:
    """Return True if a process with *pid* is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def _exit_reason(log_text: str) -> str:
    """Why the bridge died before printing a URL: its last `Error:` line, if any."""
    errors = re.findall(r"^Error:\s*(.+)$", log_text, re.MULTILINE)
    if errors:
        return errors[-1].strip()
    return "claude remote-control exited before printing a session URL"


def _poll_log_for_url(
    log_path: Path,
    timeout: float,
    debug_path: Path | None = None,
    exited: Callable[[], bool] | None = None,
) -> tuple[str | None, str | None, str | None, str]:
    """
    Poll *log_path* until the session deep link appears or *timeout* elapses.
    Returns (session_url, env_url, failure_reason, full_log_text).

    Also watches the bridge's debug file: when claude.ai refuses to create the
    session there is nothing left to wait for, and burning the full minute makes
    the UI look hung when the answer is already known. Likewise *exited*: once
    the process is gone ("folder already served", claude not on PATH, …) no URL
    is coming, and the log's last error line is the answer.
    """
    deadline = time.monotonic() + timeout
    clean = ""
    while time.monotonic() < deadline:
        if log_path.exists():
            clean = _strip_ansi(log_path.read_text(errors="replace"))
            session_url, env_url = _extract_urls(clean)
            if session_url:
                return session_url, env_url, None, clean
            if _ALREADY_SERVED in clean:
                return None, None, _exit_reason(clean), clean
        if exited is not None and exited():
            # The exit may have raced the final flush; take one last look.
            if log_path.exists():
                clean = _strip_ansi(log_path.read_text(errors="replace"))
            session_url, env_url = _extract_urls(clean)
            if session_url:
                return session_url, env_url, None, clean
            return None, None, _exit_reason(clean), clean
        if debug_path is not None and debug_path.exists():
            reason = _spawn_failure_reason(debug_path.read_text(errors="replace"))
            if reason:
                # Give the environment URL a moment to reach the log so the
                # session is still attachable, then stop waiting.
                time.sleep(1.0)
                clean = _strip_ansi(log_path.read_text(errors="replace")) if log_path.exists() else clean
                return None, _extract_urls(clean)[1], reason, clean
        time.sleep(_POLL_INTERVAL)
    if log_path.exists():
        clean = _strip_ansi(log_path.read_text(errors="replace"))
    return (None, _extract_urls(clean)[1], None, clean)


def local_bin_env() -> dict[str, str]:
    """The service env with ~/.local/bin on PATH, for anything that runs claude.

    systemd hands user services a stripped PATH that omits ~/.local/bin, so the
    `claude` installed there is only reachable if something adds it back. A
    session spawn gets it from the user's login shell only if that shell happens
    to add it (otherwise: `Unknown command: claude`, exit 127); a bare
    `claude -p` gets FileNotFoundError.

    ANTHROPIC_BASE_URL is deliberately left untouched here — see start_session.
    """
    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    path = env.get("PATH", "")
    if local_bin not in path.split(":"):
        env["PATH"] = f"{local_bin}:{path}" if path else local_bin
    return env


def _proc_stat(pid: int) -> tuple[str, int, int] | None:
    """(state, ppid, pgrp) of *pid* from /proc, or None if it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm (field 2) is parenthesised and may itself contain spaces or ')'.
    fields = stat[stat.rindex(")") + 2:].split()
    return fields[0], int(fields[1]), int(fields[2])


def _descendants(pid: int) -> list[int]:
    """All live descendants of *pid*, found by walking /proc parent links."""
    children: dict[int, list[int]] = {}
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            stat = _proc_stat(int(entry.name))
            if stat:
                children.setdefault(stat[1], []).append(int(entry.name))
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def _reap(pid: int) -> None:
    """Collect *pid*'s exit status if it is our child, so it leaves no zombie."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def _terminate(pid: int, grace: float = _KILL_GRACE_SECONDS) -> None:
    """SIGTERM *pid* and its whole process tree, wait for it, then SIGKILL.

    The tree, not the process group: `script` runs claude in a session of its
    own on the pty, so signalling script's group leaves claude to die later of
    SIGHUP — after a restart has already tried to respawn in the same folder.
    Returns only once everything is gone or has been SIGKILLed.
    """
    targets = [pid, *_descendants(pid)]

    def alive() -> list[int]:
        _reap(pid)
        return [t for t in targets if (st := _proc_stat(t)) is not None and st[0] != "Z"]

    def send(sig: signal.Signals, pids: list[int]) -> None:
        for t in pids:
            try:
                os.kill(t, sig)
            except ProcessLookupError:
                pass

    send(signal.SIGTERM, targets)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not alive():
            return
        time.sleep(0.2)
    left = alive()
    if left:
        logger.warning("pids {} still alive {}s after SIGTERM; sending SIGKILL", left, grace)
        send(signal.SIGKILL, left)


def bridge_holders(project_path: str) -> list[dict[str, Any]]:
    """Local `claude remote-control` processes serving *project_path*.

    claude refuses to start a second bridge in a folder, so anything listed here
    blocks a new session. Each entry is {pid, name, ancestors}: the bridge's own
    pid, its --name, and the pids above it (rcpilot records the `script` pid
    there, two levels up).
    """
    target = os.path.realpath(project_path)
    holders = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            argv = [a.decode(errors="replace") for a in
                    Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")]
            if len(argv) < 2 or os.path.basename(argv[0]) != "claude" or argv[1] != "remote-control":
                continue
            if os.path.realpath(f"/proc/{pid}/cwd") != target:
                continue
        except OSError:
            continue
        name = argv[argv.index("--name") + 1] if "--name" in argv[:-1] else ""
        ancestors, cur = [], pid
        for _ in range(4):
            stat = _proc_stat(cur)
            if not stat or stat[1] <= 1:
                break
            cur = stat[1]
            ancestors.append(cur)
        holders.append({"pid": pid, "name": name, "ancestors": ancestors})
    return holders


def release_bridge(project_path: str, pid: int) -> bool:
    """Stop the stray bridge *pid* serving *project_path*.

    Only a pid that is verifiably a remote-control bridge in that folder is
    touched, so a stale or forged request cannot be used to kill anything else.
    Returns False if *pid* is not such a bridge (already gone, or never was).
    """
    if not any(h["pid"] == pid for h in bridge_holders(project_path)):
        return False
    logger.info("release_bridge: stopping stray bridge pid={} in {}", pid, project_path)
    # The bridge and its sessions, not its group: one started by hand in a
    # terminal shares a group with that shell. A `script` wrapper above it exits
    # on its own once its child is gone.
    _terminate(pid)
    return True


def _bridge_pointer_relpath(project_path: str) -> str:
    """Path of the bridge-pointer file for *project_path*, relative to $HOME."""
    encoded = str(Path(project_path)).replace("/", "-")
    return f".claude/projects/{encoded}/bridge-pointer.json"


def _clear_bridge_pointer(project_path: str) -> None:
    """Remove ~/.claude/projects/<encoded-cwd>/bridge-pointer.json if it exists.

    Claude code 2.1.x caches the environmentId in this file and reuses it when
    a new `claude remote-control` runs within 4h. In --spawn=same-dir mode that
    makes every "New Session" reattach to the same environment URL. Delete it so
    each spawn registers a fresh environment and gets its own distinct URL.
    """
    encoded = str(Path(project_path).resolve()).replace("/", "-")
    pointer = Path.home() / ".claude" / "projects" / encoded / "bridge-pointer.json"
    try:
        pointer.unlink()
        logger.info("cleared bridge-pointer at {}", pointer)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("could not remove bridge-pointer {}: {}", pointer, exc)


# The bridge reports why it could not create a session (unauthorised private
# repo, expired GitHub link, …) *only* in its --debug-file. The terminal output
# stays cheerfully green, so without this the UI would show a healthy session
# whose attach button silently opens a fresh conversation instead.
_SPAWN_FAILURE_PATTERN = re.compile(r"Session creation failed with status \d+:\s*(.+)")


def _spawn_failure_reason(debug_text: str) -> str | None:
    match = _SPAWN_FAILURE_PATTERN.search(debug_text)
    return match.group(1).strip() if match else None


def _claude_command(
    claude_name: str, yolo: bool, permission_mode: str, debug_file: str | None = None
) -> str:
    """Build the `claude remote-control` command line shared by both hosts."""
    import shlex

    # Use --spawn=same-dir (not --spawn=session). As of claude 2.1.x,
    # --spawn=session prints a /session_<id> deep link that the web/mobile app
    # opens as a *cloud container*, whereas --spawn=same-dir prints a
    # ?environment=<env-id> URL that attaches to the LOCAL session running here.
    #
    # NOTE: --name only labels the pre-created session. When the user opens the
    # ?environment= URL the web app drops them into a fresh on-demand session
    # with no explicit name, so claude.ai auto-titles it from the first prompt.
    # As of claude 2.1.x there is no CLI flag that forces a fixed name onto that
    # on-demand session; the rcpilot-side name is tracked in our own DB/UI.
    cmd = f"claude remote-control --spawn=same-dir --name {shlex.quote(claude_name)}"
    # YOLO overrides the configured permission mode with bypassPermissions.
    effective_mode = "bypassPermissions" if yolo else permission_mode
    if effective_mode and effective_mode != "default":
        cmd += f" --permission-mode {effective_mode}"
    if debug_file:
        # sh_path, not shlex.quote: a remote debug path starts with '~', and
        # quoting that whole string makes the shell treat it as a literal
        # directory name — the file lands in a '~' folder inside the repo.
        cmd += f" --verbose --debug-file {host_mgr.sh_path(debug_file)}"
    return cmd


def start_session(
    project: str,
    project_path: str,
    db_name: str,
    claude_name: str,
    db_path: str,
    yolo: bool = False,
    permission_mode: str = "auto",
    host: "RemoteHost | None" = None,
) -> dict[str, Any]:
    """
    Spawn `claude remote-control` via `script`. Blocks until URL is captured or timeout.
    Returns dict with keys: status, rc_url, session_id, name.

    NOTE: remote-control sessions must NOT set ANTHROPIC_BASE_URL. As of claude
    2.1.x, `claude remote-control` refuses to start ("Remote Control is only
    available when using Claude via api.anthropic.com.") unless the base URL is
    unset or its host is exactly api.anthropic.com. So the session inherits the
    service env (see local_bin_env, which only widens PATH) and talks to Anthropic
    directly — the usage-stats proxy only sits in front of the non-interactive
    `claude -p` calls.
    """
    if host is not None:
        return _start_remote_session(
            host, project, project_path, db_name, claude_name,
            yolo, permission_mode, db_path,
        )

    db_dir = Path(db_path).parent
    log_path = db_dir / f"session-{secrets.token_hex(6)}.log"
    debug_path = log_path.with_suffix(".debug")
    claude_cmd = _claude_command(claude_name, yolo, permission_mode, str(debug_path))

    _clear_bridge_pointer(project_path)

    # Wrap in systemd-run --scope so the process lives in its own transient
    # cgroup, outside the rcpilot service cgroup.  Without this, systemd's
    # default KillMode=control-group would kill Claude when rcpilot restarts —
    # even though script is spawned with start_new_session=True.
    cmd = [
        "systemd-run", "--user", "--scope", "--",
        "script", "-q", "-e", "-f", "-c", claude_cmd, str(log_path),
    ]
    logger.info("start_session: project={} db_name={!r} claude_name={!r} log={}", project, db_name, claude_name, log_path)

    proc = subprocess.Popen(
        cmd,
        cwd=project_path,
        env=local_bin_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info("spawned script pid={}", proc.pid)

    session_url, env_url, failure, output = _poll_log_for_url(
        log_path, _URL_WAIT_SECONDS, debug_path,
        exited=lambda: proc.poll() is not None,
    )
    url = session_url or env_url

    try:
        sid = db.create_session(
            db_path, project, db_name, proc.pid, url,
            log_path=str(log_path), env_url=env_url,
        )
    except Exception:
        # A bridge we cannot record is one nobody can see or stop — and it holds
        # the folder, so every later start there fails. Take it down with us.
        logger.exception("start_session: could not record session; stopping pid {}", proc.pid)
        _terminate(proc.pid)
        raise

    if url is None:
        # Whatever is still running never produced a URL; leaving it up would
        # orphan a bridge that blocks the folder.
        _terminate(proc.pid)
        error = failure or f"no session URL within {_URL_WAIT_SECONDS}s"
        logger.warning("session start failed ({}). Log:\n{}", error, output.strip() or "(empty)")
        db.end_session(db_path, sid, "timed_out", output or None)
        result: dict[str, Any] = {
            "status": "timed_out", "rc_url": None, "session_id": sid,
            "name": db_name, "error": error,
        }
        if _ALREADY_SERVED in error:
            result["holder"] = _describe_holder(project_path, project, db_path)
        return result

    warning = failure
    if session_url is None:
        # Attaching lands the user in a fresh session rather than this one.
        logger.warning(
            "no session deep link appeared ({}); falling back to environment URL {}",
            warning or "reason unknown", env_url,
        )
    logger.info("RC URL captured: {}", url)
    return {
        "status": "running", "rc_url": url, "session_id": sid,
        "name": db_name, "warning": warning,
    }


def _describe_holder(project_path: str, project: str, db_path: str) -> dict[str, Any] | None:
    """Who is serving *project_path*, for the "folder already served" error.

    `tracked` says whether rcpilot knows the bridge (a running session of this
    project); an untracked one is a stray the UI can offer to stop.
    """
    holders = bridge_holders(project_path)
    if not holders:
        return None
    holder = holders[0]
    running = {r["pid"]: r for r in db.list_running_sessions(db_path, project) if r.get("pid")}
    owner = next((running[a] for a in [holder["pid"], *holder["ancestors"]] if a in running), None)
    return {
        "pid": holder["pid"],
        "name": owner["name"] if owner else holder["name"],
        "tracked": owner is not None,
    }


# ---------------------------------------------------------------------------
# Remote sessions
# ---------------------------------------------------------------------------

# Start the session in a transient unit, then wait for the URL to land in the
# log — both in one ssh round trip. The unit is detached from this connection,
# so a drop mid-wait costs us the URL, not the session.
_REMOTE_START = r"""
mkdir -p ~/.cache/rcpilot
rm -f ~/{pointer}
err=$(systemd-run --user --collect --unit={unit} bash -lc {inner} 2>&1 >/dev/null)
if [ $? -ne 0 ]; then
  printf 'RCPILOT_SPAWN_FAILED %s\n' "$err"
  exit 0
fi
i=0
while [ $i -lt {ticks} ]; do
  grep -qa 'claude\.ai/code/session_' {log} 2>/dev/null && break
  if grep -qa 'Session creation failed' {debug} 2>/dev/null; then sleep 1; break; fi
  sleep {interval}
  i=$((i+1))
done
cat {log} 2>/dev/null
printf '\n{sep}\n'
grep -a 'Session creation failed' {debug} 2>/dev/null | tail -1
"""

# Separates the terminal capture from the bridge's own diagnostics in the single
# blob the remote start script sends back.
_REMOTE_DEBUG_SEP = "===RCPILOT-DEBUG==="


def _start_remote_session(
    host: "RemoteHost",
    project: str,
    project_path: str,
    db_name: str,
    claude_name: str,
    yolo: bool,
    permission_mode: str,
    db_path: str,
) -> dict[str, Any]:
    """Spawn a session on *host* and register it. Mirrors the local start path."""
    import shlex

    token = secrets.token_hex(6)
    unit = f"rcpilot-session-{token}"
    log_path = f"~/.cache/rcpilot/session-{token}.log"
    debug_path = f"~/.cache/rcpilot/session-{token}.debug"
    quoted_log = host_mgr.sh_path(log_path)
    claude_cmd = _claude_command(claude_name, yolo, permission_mode, debug_path)

    inner = (
        f"cd {host_mgr.sh_path(project_path)} && "
        f"exec script -q -e -f -c {shlex.quote(claude_cmd)} {quoted_log}"
    )
    script = _REMOTE_START.format(
        pointer=shlex.quote(_bridge_pointer_relpath(project_path)),
        unit=unit,
        inner=shlex.quote(inner),
        log=quoted_log,
        debug=host_mgr.sh_path(debug_path),
        sep=_REMOTE_DEBUG_SEP,
        ticks=int(_URL_WAIT_SECONDS / _POLL_INTERVAL),
        interval=_POLL_INTERVAL,
    )
    logger.info(
        "start_session: host={} project={} db_name={!r} unit={}", host.name, project, db_name, unit
    )

    try:
        proc = host_mgr.run_script(host, script, timeout=_URL_WAIT_SECONDS + 30)
    except host_mgr.HostUnreachable as exc:
        logger.error("start_session: {}", exc)
        return {"status": "error", "rc_url": None, "session_id": None,
                "name": db_name, "detail": str(exc)}

    output, _, debug_tail = _strip_ansi(proc.stdout).partition(_REMOTE_DEBUG_SEP)
    if output.startswith("RCPILOT_SPAWN_FAILED"):
        detail = output.split(" ", 1)[-1].strip()
        logger.error("start_session: systemd-run failed on {}: {}", host.name, detail)
        return {"status": "error", "rc_url": None, "session_id": None,
                "name": db_name, "detail": detail}

    session_url, env_url = _extract_urls(output)
    url = session_url or env_url
    sid = db.create_session(
        db_path, project, db_name, pid=None, rc_url=url,
        log_path=log_path, unit=f"{unit}.service", env_url=env_url,
    )

    if url is None:
        logger.warning(
            "timed out waiting for RC URL on {}. Log:\n{}", host.name, output.strip() or "(empty)"
        )
        db.end_session(db_path, sid, "timed_out", output or None)
        return {"status": "timed_out", "rc_url": None, "session_id": sid, "name": db_name}

    warning = None
    if session_url is None:
        warning = _spawn_failure_reason(debug_tail)
        logger.warning(
            "no session deep link appeared on {} ({}); falling back to environment URL {}",
            host.name, warning or "reason unknown", env_url,
        )
    logger.info("RC URL captured on {}: {}", host.name, url)
    return {
        "status": "running", "rc_url": url, "session_id": sid,
        "name": db_name, "warning": warning,
    }


# Unit state, log mtime and the newest session deep link, one line per session
# and all batched into a single ssh call.
_REMOTE_PROBE = """
probe() {
  printf '%s\\t%s\\t%s\\t%s\\n' "$1" \\
    "$(systemctl --user is-active "$2" 2>/dev/null || true)" \\
    "$(stat -c %Y "$3" 2>/dev/null || echo 0)" \\
    "$(grep -aoE 'https://claude\\.ai/code/session_[A-Za-z0-9_-]+' "$3" 2>/dev/null | tail -1)"
}
"""


def probe_remote(
    host: "RemoteHost", records: list[dict[str, Any]]
) -> dict[int, dict[str, Any]] | None:
    """Liveness, log mtime and current session URL for *records* on *host*.

    Returns None when the host could not be reached, meaning "unknown" — callers
    must leave those sessions alone rather than marking them stopped.
    """
    import shlex

    probes = [r for r in records if r.get("unit")]
    if not probes:
        return {}
    lines = [
        f"probe {r['id']} {shlex.quote(r['unit'])} {host_mgr.sh_path(r.get('log_path') or '/nonexistent')}"
        for r in probes
    ]
    try:
        proc = host_mgr.run_script(host, _REMOTE_PROBE + "\n".join(lines) + "\n", timeout=20.0)
    except host_mgr.HostUnreachable as exc:
        logger.warning("cannot probe sessions on {}: {}", host.name, exc)
        return None
    if proc.returncode != 0:
        logger.warning("probe on {} failed: {}", host.name, proc.stderr.strip())
        return None

    status: dict[int, dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        sid, state, mtime = fields[0], fields[1], fields[2]
        session_url = fields[3].strip() if len(fields) > 3 else ""
        try:
            status[int(sid)] = {
                "alive": state.strip() == "active",
                "mtime": float(mtime or 0),
                "session_url": session_url or None,
            }
        except ValueError:
            continue
    # A record systemd never answered for is genuinely unknown, not dead.
    return status if len(status) == len(probes) else None


_REMOTE_KILL = """
systemctl --user stop {unit} 2>/dev/null || true
sleep 0.5
cat {log} 2>/dev/null
"""


def _session_url_from_log(record: dict[str, Any]) -> str | None:
    """Newest session deep link in a local session's log, if any."""
    log_path = record.get("log_path")
    if not log_path:
        return None
    try:
        text = Path(log_path).read_text(errors="replace")
    except OSError:
        return None
    return _extract_urls(_strip_ansi(text))[0]


def _repaired_url(db_path: str, record: dict[str, Any], session_url: str | None) -> str:
    """Upgrade a stored environment URL to the session deep link, once.

    Sessions started before rcpilot learned the difference have an
    ``?environment=`` URL on record, which attaches to the directory rather than
    to the conversation. Rewrite those in place the first time we see the real
    link; sessions already pointing at a ``/session_`` URL are left alone.
    """
    current = record.get("rc_url") or ""
    if not session_url or session_url == current or "/session_" in current:
        return current
    db.update_session_url(db_path, record["id"], session_url)
    logger.info(
        "session {}: attach URL repaired to session deep link {}", record["id"], session_url
    )
    return session_url


def _last_activity(log_path: str | None, fallback: str) -> str:
    """ISO-8601 UTC time of the session's most recent activity.

    `script` writes PTY output to the log file continuously, so its mtime tracks
    the latest terminal activity. Imported sessions have no log file — fall back
    to started_at.
    """
    if log_path:
        try:
            mtime = os.path.getmtime(log_path)
            return datetime.fromtimestamp(mtime, timezone.utc).isoformat()
        except OSError:
            pass
    return fallback


def list_running_sessions(
    project: str, db_path: str, host: "RemoteHost | None" = None
) -> list[dict[str, Any]]:
    """
    Return all live running sessions for *project*.
    Auto-marks stale DB records (process gone) as stopped.
    Imported sessions (no pid) are always considered alive.
    """
    records = db.list_running_sessions(db_path, project)
    remote_status = probe_remote(host, records) if host is not None else None
    result = []
    for record in records:
        pid = record.get("pid")
        is_imported = bool(record.get("imported"))
        started_at = record.get("started_at") or ""
        last_activity = _last_activity(record.get("log_path"), started_at)
        rc_url = record["rc_url"]

        if is_imported:
            alive = True
        elif host is not None:
            if remote_status is None:
                # Host unreachable — assume alive; the session outlives the link.
                alive = True
            else:
                probe = remote_status.get(record["id"], {})
                alive = bool(probe.get("alive"))
                if probe.get("mtime"):
                    last_activity = datetime.fromtimestamp(
                        probe["mtime"], timezone.utc
                    ).isoformat()
                rc_url = _repaired_url(db_path, record, probe.get("session_url"))
        else:
            alive = bool(pid and _pid_alive(pid))
            if alive:
                rc_url = _repaired_url(db_path, record, _session_url_from_log(record))

        if alive:
            result.append({
                "id": record["id"],
                "name": record["name"] or "",
                "rc_url": rc_url,
                "env_url": record.get("env_url"),
                "status": "running",
                "imported": is_imported,
                "started_at": started_at,
                "last_activity": last_activity,
            })
        else:
            logger.debug("stale running record {} — process gone, marking stopped", record["id"])
            db.mark_session_stopped(db_path, record["id"])
    return result


def resume_session(
    session_id: int,
    project: str,
    project_path: str,
    db_path: str,
    yolo: bool = False,
    permission_mode: str = "auto",
    host: "RemoteHost | None" = None,
) -> dict[str, Any]:
    """
    Start a new session as a continuation of a previous one.
    Looks up the old session name and prefixes it with 'cont.' for the new session.
    """
    record = db.get_session_by_id(db_path, session_id)
    if not record:
        return {"status": "error", "rc_url": None, "session_id": None, "name": None}
    old_name = record.get("name") or record.get("started_at", "")[:10]
    db_name = f"cont. {old_name}"
    claude_name = f"{project} - {db_name}"
    return start_session(
        project, project_path, db_name, claude_name, db_path,
        yolo=yolo, permission_mode=permission_mode, host=host,
    )


def kill_session(
    session_id: int, db_path: str, host: "RemoteHost | None" = None
) -> dict[str, Any]:
    """
    Terminate a session by SIGTERMing the script process tree — or, for a
    session on a remote host, by stopping its systemd unit.
    Reads the log file for a snapshot before cleaning up.
    Imported sessions have no pid; they are just marked stopped.
    """
    record = db.get_session_by_id(db_path, session_id)
    if not record:
        logger.warning("kill_session: session {} not found in DB", session_id)
        return {"status": "stopped"}

    pid = record.get("pid")
    log_path_str = record.get("log_path")
    snapshot: str | None = None

    if host is not None:
        snapshot = _kill_remote(host, record)
    else:
        if pid and _pid_alive(pid):
            # Kill script + claude and wait them out: a restart respawns in the
            # same folder straight after this.
            _terminate(pid)

        if log_path_str:
            log_path = Path(log_path_str)
            if log_path.exists():
                snapshot = _strip_ansi(log_path.read_text(errors="replace"))

    db.end_session(db_path, session_id, "stopped", snapshot)
    logger.info("kill_session: id={} pid={} host={}", session_id, pid, host.name if host else "local")
    return {"status": "stopped"}


def _kill_remote(host: "RemoteHost", record: dict[str, Any]) -> str | None:
    """Stop a remote session's unit and return its log as a snapshot."""
    import shlex

    unit = record.get("unit")
    if not unit:
        return None
    script = _REMOTE_KILL.format(
        unit=shlex.quote(unit),
        log=host_mgr.sh_path(record.get("log_path") or "/nonexistent"),
    )
    try:
        proc = host_mgr.run_script(host, script, timeout=25.0)
    except host_mgr.HostUnreachable as exc:
        # The record is still marked stopped: the caller asked for it gone, and
        # a session we cannot reach is no longer useful to show as attachable.
        logger.warning("kill_session: could not reach {}: {}", host.name, exc)
        return None
    return _strip_ansi(proc.stdout) or None


def import_session(
    project: str,
    rc_url: str,
    db_name: str,
    db_path: str,
) -> dict[str, Any]:
    """
    Register an externally-started RC session (e.g. from the IDE) by its URL.
    No process is spawned — the session is stored as imported with no pid.
    """
    logger.info("import_session: project={} db_name={!r} rc_url={}", project, db_name, rc_url)
    sid = db.create_session(
        db_path, project, db_name, pid=None, rc_url=rc_url, imported=True
    )
    return {"status": "running", "rc_url": rc_url, "session_id": sid, "name": db_name}
