"""
rcpilot FastAPI application.

Routes (all sync — FastAPI runs them in a thread pool):
  GET    /                                  → serve index.html
  GET    /api/projects                      → list projects
  POST   /api/sessions/{project}            → start new session, return {status, rc_url, session_id, name}
  GET    /api/sessions/{project}            → list running sessions [{id, name, rc_url, status}]
  DELETE /api/sessions/{project}/{id}       → kill a specific session by id
  GET    /api/sessions/{project}/history    → list past sessions for project
  GET    /api/sessions/{project}/history/{session_id}/snapshot → session output snapshot
  PATCH  /api/sessions/{project}/history/{session_id}          → rename session
  DELETE /api/sessions/{project}/history                       → clear non-running sessions
  POST   /api/projects/{project}/run-claude                   → run claude -p in project dir
  POST   /api/projects/{project}/review-pr                   → fetch GitHub PR via gh CLI and review with claude -p
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import secrets
import subprocess
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

import tomllib as _tomllib

def _read_version() -> str:
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    try:
        with open(pyproject, "rb") as f:
            return _tomllib.load(f)["project"]["version"]
    except Exception:
        from importlib.metadata import version as pkg_version
        return pkg_version("rcpilot")

from pilot.config import Config, load_config
from pilot.projects import list_projects
from pilot import db as pilot_db
from pilot import proxy as pilot_proxy
from pilot import sessions as session_mgr
from pilot.watchdog import start_watchdog
from pilot.ticker import start_ticker, get_ticker_state
from pilot.updater import start_updater, get_updater_state, force_update
from pilot.self_updater import start_self_updater, get_self_updater_state, force_self_upgrade

_config: Config = load_config()
_STATIC_DIR = Path(__file__).parent / "static"

_SESSION_SECRET = (
    hmac.new(_config.admin_keyphrase.encode(), b"rcpilot_session_secret_v1", hashlib.sha256).digest()
    if _config.admin_keyphrase
    else secrets.token_bytes(32)
)
_SESSION_TOKEN = hmac.new(_SESSION_SECRET, b"rcpilot_authenticated", hashlib.sha256).hexdigest()
_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
_CODE_REVIEW_PLUGIN = "code-review@claude-plugins-official"


def _ensure_code_review_plugin() -> None:
    """Enable the code-review plugin in ~/.claude/settings.json if not already present."""
    import json
    settings: dict = {}
    if _CLAUDE_SETTINGS.exists():
        try:
            settings = json.loads(_CLAUDE_SETTINGS.read_text())
        except Exception:
            pass
    plugins: dict = settings.setdefault("enabledPlugins", {})
    if plugins.get(_CODE_REVIEW_PLUGIN) is True:
        return
    plugins[_CODE_REVIEW_PLUGIN] = True
    _CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    _CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    logger.info("enabled {} in {}", _CODE_REVIEW_PLUGIN, _CLAUDE_SETTINGS)


# ---------------------------------------------------------------------------
# Lifespan — DB init + watchdog start
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    logger.info("rcpilot v{}", _read_version())
    db_path = str(_config.db_path)
    logger.info("initialising database at {}", db_path)
    _config.db_path.parent.mkdir(parents=True, exist_ok=True)
    await pilot_db.init_db(db_path)
    _ensure_code_review_plugin()

    logger.info("starting watchdog")
    _wd_thread, _wd_stop = start_watchdog(_config)

    logger.info("starting ticker (window_cron={!r})", _config.window_cron)
    _tk_thread, _tk_stop = start_ticker(_config)

    logger.info("starting updater (claude_update_cron={!r})", _config.claude_update_cron)
    _up_thread, _up_stop = start_updater(_config)

    _current_version = _read_version()
    logger.info("starting self-updater (mode={!r})", _config.rcpilot_update_mode)
    _su_thread, _su_stop = start_self_updater(_config, _current_version)

    yield  # server runs here

    logger.info("shutting down — signalling watchdog, ticker, updater and self-updater")
    _wd_stop.set()
    _wd_thread.join(timeout=5)
    _tk_stop.set()
    _tk_thread.join(timeout=5)
    _up_stop.set()
    _up_thread.join(timeout=5)
    _su_stop.set()
    _su_thread.join(timeout=5)


app = FastAPI(title="rcpilot", version=_read_version(), lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Minimal app for the HTTP proxy-only server (no lifespan, no auth, localhost only).
# Used when TLS is enabled so ANTHROPIC_BASE_URL stays plain HTTP.
_proxy_app = FastAPI()


@_proxy_app.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def _proxy_app_handler(request: Request, path: str) -> StreamingResponse:
    return await pilot_proxy.handle(request, path)


# ---------------------------------------------------------------------------
# Auth middleware + endpoints
# ---------------------------------------------------------------------------

@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    if not _config.admin_keyphrase:
        return await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") or path.startswith("/api/auth/"):
        return await call_next(request)
    cookie = request.cookies.get("rcpilot_session", "")
    if secrets.compare_digest(cookie.encode(), _SESSION_TOKEN.encode()):
        return await call_next(request)
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    enabled = bool(_config.admin_keyphrase)
    if not enabled:
        return {"enabled": False, "authenticated": True}
    cookie = request.cookies.get("rcpilot_session", "")
    authenticated = secrets.compare_digest(cookie.encode(), _SESSION_TOKEN.encode())
    return {"enabled": enabled, "authenticated": authenticated}


@app.post("/api/auth/login")
def auth_login(request: Request, body: dict = Body(...)) -> JSONResponse:
    if not _config.admin_keyphrase:
        return JSONResponse({"authenticated": True})
    keyphrase = str(body.get("keyphrase", ""))
    if not secrets.compare_digest(keyphrase.encode(), _config.admin_keyphrase.encode()):
        raise HTTPException(status_code=401, detail="Invalid keyphrase")
    response = JSONResponse({"authenticated": True})
    secure = bool(_config.ssl_certfile and _config.ssl_keyfile)
    response.set_cookie(
        "rcpilot_session",
        _SESSION_TOKEN,
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=7 * 24 * 3600,
    )
    return response


@app.post("/api/auth/logout")
def auth_logout() -> JSONResponse:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie("rcpilot_session", httponly=True, samesite="strict")
    return response


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def serve_index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/sw.js", include_in_schema=False)
def serve_sw() -> FileResponse:
    return FileResponse(_STATIC_DIR / "sw.js", media_type="application/javascript")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.get("/api/info")
def get_info() -> dict:
    updater = get_updater_state()
    self_updater = get_self_updater_state()
    return {
        "version": _read_version(),
        "claude_version": updater["claude_version"],
        "last_check_at": updater["last_check_at"],
        "last_update_at": updater["last_update_at"],
        "rcpilot_update_available": self_updater["update_available"],
        "rcpilot_latest_version": self_updater["latest_version"],
    }


def _resolved_proxy_port() -> int:
    """HTTP-only proxy port — used when TLS is active so ANTHROPIC_BASE_URL stays plain HTTP."""
    if _config.proxy_port:
        return _config.proxy_port
    return _config.port + 1


def _proxy_url() -> str:
    tls = bool(_config.ssl_certfile and _config.ssl_keyfile)
    port = _resolved_proxy_port() if tls else _config.port
    return f"http://127.0.0.1:{port}/proxy"


def _subprocess_env() -> dict:
    """Return an env dict suitable for claude subprocesses.

    Extends the service PATH with ~/.local/bin so that `claude` (installed
    there by npm/pip for the pi user) is reachable even when the systemd
    service starts with a stripped PATH.
    """
    import os
    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    path = env.get("PATH", "")
    if local_bin not in path.split(":"):
        env["PATH"] = f"{local_bin}:{path}"
    env["ANTHROPIC_BASE_URL"] = _proxy_url()
    return env


@app.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def anthropic_proxy(request: Request, path: str) -> StreamingResponse:
    """Transparent proxy to api.anthropic.com — captures usage stats."""
    return await pilot_proxy.handle(request, path)


@app.get("/api/stats")
def get_stats() -> dict:
    """Return current Anthropic usage stats captured by the proxy."""
    return pilot_proxy.get_stats()


@app.post("/api/update")
def trigger_update() -> dict:
    """Trigger an immediate claude update in the background."""
    force_update()
    return {"ok": True}


@app.post("/api/rcpilot-update")
def trigger_self_update() -> dict:
    """Upgrade rcpilot to the latest PyPI version and restart the service."""
    force_self_upgrade()
    return {"ok": True}


def _get_config_path() -> Path:
    import os
    from pilot.config import DEFAULT_CONFIG_PATH
    env_path = os.environ.get("PILOT_CONFIG")
    return Path(env_path) if env_path else DEFAULT_CONFIG_PATH


def _update_config_toml(updates: dict) -> None:
    """Update specific keys in the TOML config file, preserving comments."""
    import re
    path = _get_config_path()
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    written_keys: set[str] = set()
    result = []
    for line in lines:
        m = re.match(r'^(\w+)\s*=', line)
        if m and m.group(1) in updates:
            key = m.group(1)
            val = updates[key]
            result.append(f'{key} = "{val}"\n' if isinstance(val, str) else f'{key} = {val}\n')
            written_keys.add(key)
        else:
            result.append(line)
    for key, val in updates.items():
        if key not in written_keys:
            result.append(f'{key} = "{val}"\n' if isinstance(val, str) else f'{key} = {val}\n')
    path.write_text(''.join(result))


@app.get("/api/config")
def get_config_values() -> dict:
    """Return current configuration values."""
    return {
        "projects_dir": str(_config.projects_dir),
        "host": _config.host,
        "port": _config.port,
        "db_path": str(_config.db_path),
        "window_cron": _config.window_cron,
        "claude_update_cron": _config.claude_update_cron,
        "rcpilot_update_mode": _config.rcpilot_update_mode,
        "permission_mode": _config.permission_mode,
    }


@app.post("/api/restart")
def restart_service() -> dict:
    """Schedule a service restart in 1 second so the HTTP response is delivered first."""
    import threading
    import time

    def _do_restart() -> None:
        time.sleep(1)
        subprocess.Popen(
            ["systemctl", "--user", "restart", "rcpilot.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    threading.Thread(target=_do_restart, daemon=True).start()
    return {"ok": True}


@app.put("/api/config")
def update_config_values(body: dict = Body(...)) -> dict:
    """Persist updated config values to the TOML file. Restart required to take effect."""
    allowed = {"projects_dir", "host", "port", "window_cron", "claude_update_cron", "rcpilot_update_mode"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=422, detail="No valid fields provided")
    # Coerce port to int if present
    if "port" in updates:
        try:
            updates["port"] = int(updates["port"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="port must be an integer")
    _update_config_toml(updates)
    return {"ok": True}


@app.get("/api/ticker")
def get_ticker() -> dict:
    """Return window ticker state: configured times, last fired, next window."""
    state = get_ticker_state()
    # Use the authoritative reset timestamp from Anthropic headers when available
    if not state.get("reset_at"):
        proxy_resets = pilot_proxy.get_stats().get("resets", {})
        if "5h" in proxy_resets:
            state["reset_at"] = proxy_resets["5h"]
    return state


@app.get("/api/projects")
def get_projects(sort_by: str = "modified") -> list[dict]:
    return list_projects(_config.projects_dir, sort_by=sort_by)  # type: ignore[return-value]


@app.get("/api/projects/{project}/git-log")
def git_log(project: str) -> dict:
    path = _get_project_path(project)
    result = subprocess.run(
        ["git", "log", "--oneline", "--graph", "--decorate", "-50"],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {"log": result.stdout}


@app.get("/api/projects/{project}/git-diff")
def git_diff(project: str) -> dict:
    path = _get_project_path(project)
    result = subprocess.run(
        ["git", "diff"],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {"diff": result.stdout}


@app.post("/api/projects/{project}/run-claude")
def run_claude(
    project: str,
    prompt: str = Body(..., embed=True),
) -> dict:
    """Run ``claude -p <prompt>`` in the project directory and return the output."""
    path = _get_project_path(project)
    result = subprocess.run(
        ["claude", "-p", "--dangerously-skip-permissions", prompt],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=300,
        env=_subprocess_env(),
    )
    return {
        "output": result.stdout.strip(),
        "error": result.stderr.strip(),
        "returncode": result.returncode,
    }


@app.post("/api/projects/{project}/review-pr")
def review_pr(
    project: str,
    pr_number: int = Body(..., embed=True),
) -> dict:
    """Kick off a PR code review in the background.

    The code-review plugin posts its findings as a GitHub PR comment, so we
    launch the claude process detached and return immediately rather than
    waiting up to several minutes for it to finish.
    """
    path = _get_project_path(project)
    subprocess.Popen(
        ["claude", "-p", f"/code-review:code-review {pr_number}"],
        cwd=path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_subprocess_env(),
    )
    return {
        "review": f"Review started for PR #{pr_number}. Results will be posted as a comment on the GitHub PR.",
        "error": "",
        "returncode": 0,
    }


@app.post("/api/projects/{project}/git-pull")
def git_pull(project: str, stash: bool = False) -> dict:
    path = _get_project_path(project)

    # Check for pending changes
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    has_changes = bool(status.stdout.strip())

    if has_changes and not stash:
        return {"needs_stash": True}

    stash_applied = False
    if has_changes and stash:
        stash_result = subprocess.run(
            ["git", "stash"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if stash_result.returncode != 0:
            return {
                "returncode": stash_result.returncode,
                "stdout": stash_result.stdout.strip(),
                "stderr": stash_result.stderr.strip(),
            }
        stash_applied = True

    result = subprocess.run(
        ["git", "pull", "--rebase"],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        # Abort the rebase to leave the repo in a clean state
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=path,
            capture_output=True,
            timeout=10,
        )

    if stash_applied:
        pop_result = subprocess.run(
            ["git", "stash", "pop"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        pop_stdout = pop_result.stdout.strip()
        pop_stderr = pop_result.stderr.strip()
        combined_stdout = "\n".join(filter(None, [result.stdout.strip(), pop_stdout]))
        combined_stderr = "\n".join(filter(None, [result.stderr.strip(), pop_stderr]))
        return {
            "returncode": result.returncode if result.returncode != 0 else pop_result.returncode,
            "stdout": combined_stdout,
            "stderr": combined_stderr,
        }

    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _accept_claude_trust(project_path: Path) -> None:
    """Write hasTrustDialogAccepted=true into ~/.claude.json for the given project path.

    Claude Code asks "Do you trust this folder?" on first open. Without this entry the
    remote-control session blocks waiting for user input that never arrives.
    """
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


@app.post("/api/projects/import")
def import_project(
    repo_url: str = Body(..., embed=True),
) -> dict:
    """Clone a GitHub repository into the projects directory."""
    import re
    
    # Validate GitHub URL format
    if not repo_url:
        raise HTTPException(status_code=422, detail="Repository URL is required")
    
    # Extract repo name from URL (e.g., https://github.com/user/repo.git -> repo)
    match = re.search(r'/([^/]+?)(\.git)?$', repo_url.rstrip('/'))
    if not match:
        raise HTTPException(status_code=422, detail="Invalid repository URL format")
    
    repo_name = match.group(1)
    target_path = _config.projects_dir / repo_name
    
    # Check if project already exists
    if target_path.exists():
        raise HTTPException(status_code=409, detail=f"Project '{repo_name}' already exists")
    
    # Ensure projects directory exists
    _config.projects_dir.mkdir(parents=True, exist_ok=True)
    
    # Clone the repository
    try:
        result = subprocess.run(
            ["git", "clone", repo_url, str(target_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        
        if result.returncode != 0:
            # Clean up partial clone if it failed
            if target_path.exists():
                import shutil
                shutil.rmtree(target_path)
            raise HTTPException(
                status_code=500,
                detail=f"Git clone failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        
        _accept_claude_trust(target_path)
        logger.info("imported project {} from {}", repo_name, repo_url)
        return {
            "success": True,
            "project_name": repo_name,
            "message": f"Successfully imported {repo_name}",
        }
    except subprocess.TimeoutExpired:
        # Clean up partial clone
        if target_path.exists():
            import shutil
            shutil.rmtree(target_path)
        raise HTTPException(status_code=504, detail="Clone operation timed out")
    except Exception as e:
        # Clean up partial clone
        if target_path.exists():
            import shutil
            shutil.rmtree(target_path)
        logger.error("failed to import project: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/projects/create")
def create_project(name: str = Body(..., embed=True)) -> dict:
    """Create a blank project directory."""
    import re
    if not name:
        raise HTTPException(status_code=422, detail="Project name is required")
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', name):
        raise HTTPException(status_code=422, detail="Invalid project name")
    target_path = _config.projects_dir / name
    if target_path.exists():
        raise HTTPException(status_code=409, detail=f"Project '{name}' already exists")
    _config.projects_dir.mkdir(parents=True, exist_ok=True)
    target_path.mkdir()
    _accept_claude_trust(target_path)
    logger.info("created blank project {}", name)
    return {"success": True, "project_name": name}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _get_project_path(project: str) -> str:
    """Look up a project by name; raise 404 if not found."""
    projects = {p["name"]: p for p in list_projects(_config.projects_dir)}
    if project not in projects:
        raise HTTPException(status_code=404, detail=f"Project '{project}' not found")
    return projects[project]["path"]


@app.post("/api/sessions/{project}")
def start_session(
    project: str,
    name: str = Body("", embed=True),
    yolo: bool = Body(False, embed=True),
) -> dict:
    path = _get_project_path(project)
    _accept_claude_trust(Path(path))
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    db_name = name if name else ts
    claude_name = f"{project} - {db_name}"
    return session_mgr.start_session(
        project=project,
        project_path=path,
        db_name=db_name,
        claude_name=claude_name,
        db_path=str(_config.db_path),
        yolo=yolo,
        proxy_url=_proxy_url(),
        permission_mode=_config.permission_mode,
    )


@app.post("/api/sessions/{project}/import")
def import_session(
    project: str,
    rc_url: str = Body(..., embed=True),
    name: str = Body("", embed=True),
) -> dict:
    """Register an externally-started RC session by pasting its URL."""
    _get_project_path(project)  # 404 if unknown project
    rc_url = rc_url.strip()
    if not rc_url:
        raise HTTPException(status_code=422, detail="rc_url is required")
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    db_name = name.strip() if name.strip() else ts
    return session_mgr.import_session(
        project=project,
        rc_url=rc_url,
        db_name=db_name,
        db_path=str(_config.db_path),
    )


@app.post("/api/sessions/{project}/history/{session_id}/resume")
def resume_session(
    project: str,
    session_id: int,
    yolo: bool = Body(False, embed=True),
) -> dict:
    path = _get_project_path(project)
    return session_mgr.resume_session(
        session_id=session_id,
        project=project,
        project_path=path,
        db_path=str(_config.db_path),
        yolo=yolo,
        proxy_url=_proxy_url(),
        permission_mode=_config.permission_mode,
    )


@app.get("/api/sessions/{project}/history")
def get_history(project: str) -> list[dict]:
    """Return all past session records for *project*, newest first."""
    _get_project_path(project)  # 404 if unknown project
    return pilot_db.list_sessions(str(_config.db_path), project)


@app.delete("/api/sessions/{project}/history")
def clear_history(project: str) -> dict:
    """Delete all non-running sessions (and their logs) for *project*."""
    _get_project_path(project)  # 404 if unknown project
    deleted = pilot_db.clear_history(str(_config.db_path), project)
    return {"deleted": deleted}


@app.get("/api/sessions/{project}/history/{session_id}/snapshot")
def get_session_snapshot(project: str, session_id: int) -> dict:
    """Return the pane snapshot for a specific session."""
    _get_project_path(project)  # 404 if unknown project
    content = pilot_db.get_session_snapshot(str(_config.db_path), session_id)
    return {"content": content}


@app.patch("/api/sessions/{project}/history/{session_id}")
def rename_session(
    project: str,
    session_id: int,
    name: str = Body(..., embed=True),
) -> dict:
    """Rename a session."""
    _get_project_path(project)  # 404 if unknown project
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name must not be empty")
    updated = pilot_db.rename_session(str(_config.db_path), session_id, name)
    if not updated:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}



@app.get("/api/sessions/{project}")
def get_sessions(project: str) -> dict:
    """Return all live running sessions for *project*."""
    _get_project_path(project)  # 404 if unknown project
    sessions = session_mgr.list_running_sessions(
        project=project,
        db_path=str(_config.db_path),
    )
    return {"sessions": sessions}


@app.delete("/api/sessions/{project}/{session_id}")
def delete_session(project: str, session_id: int) -> dict:
    """Kill a specific session by ID."""
    _get_project_path(project)  # 404 if unknown project
    return session_mgr.kill_session(
        session_id=session_id,
        db_path=str(_config.db_path),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    log_file = _config.db_path.parent / "pilot.log"
    logger.add(log_file, rotation="10 MB", retention=3, level="DEBUG")
    logger.info("starting rcpilot on {}:{}", _config.host, _config.port)
    logger.info("projects_dir={} db={} log={}", _config.projects_dir, _config.db_path, log_file)
    tls = bool(_config.ssl_certfile and _config.ssl_keyfile)
    if tls:
        proxy_port = _resolved_proxy_port()
        logger.info("TLS enabled: cert={} key={}", _config.ssl_certfile, _config.ssl_keyfile)
        logger.info("HTTP proxy server for ANTHROPIC_BASE_URL on 127.0.0.1:{}", proxy_port)
        t = threading.Thread(
            target=lambda: uvicorn.run(_proxy_app, host="127.0.0.1", port=proxy_port),
            daemon=True,
        )
        t.start()
    if _config.admin_keyphrase:
        logger.info("admin keyphrase auth enabled")
    uvicorn.run(
        "pilot.main:app",
        host=_config.host,
        port=_config.port,
        reload=False,
        **({"ssl_certfile": _config.ssl_certfile, "ssl_keyfile": _config.ssl_keyfile} if tls else {}),
    )


if __name__ == "__main__":
    run()
