"""
claude-pilot FastAPI application.

Routes (all sync — FastAPI runs them in a thread pool):
  GET    /                                  → serve index.html
  GET    /api/projects                      → list projects
  POST   /api/sessions/{project}            → start new session, return {status, rc_url, session_id, name}
  GET    /api/sessions/{project}            → list running sessions [{id, name, rc_url, status}]
  DELETE /api/sessions/{project}/{id}       → kill a specific session by id
  GET    /api/sessions/{project}/history    → list past sessions for project
  GET    /api/logs?since=<cursor>           → recent log lines for browser console forwarding
  GET    /api/sessions/{project}/history/{session_id}/snapshot → pane snapshot
  PATCH  /api/sessions/{project}/history/{session_id}          → rename session
  DELETE /api/sessions/{project}/history                       → clear non-running sessions
  POST   /api/sessions/{project}/send                         → send keys to active tmux session
  POST   /api/projects/{project}/run-claude                   → run claude -p in project dir
  POST   /api/projects/{project}/review-pr                   → fetch GitHub PR via gh CLI and review with claude -p
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from importlib.metadata import version as pkg_version

from pilot.config import Config, load_config
from pilot.projects import list_projects
from pilot import db as pilot_db
from pilot import sessions as session_mgr
from pilot.watchdog import start_watchdog

_config: Config = load_config()
_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Lifespan — DB init + watchdog start
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    db_path = str(_config.db_path)
    logger.info("initialising database at {}", db_path)
    _config.db_path.parent.mkdir(parents=True, exist_ok=True)
    await pilot_db.init_db(db_path)

    logger.info("starting watchdog")
    _thread, _stop = start_watchdog(_config)

    yield  # server runs here

    logger.info("shutting down — signalling watchdog")
    _stop.set()
    _thread.join(timeout=5)


app = FastAPI(title="claude-pilot", version="0.1.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def serve_index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.get("/api/info")
def get_info() -> dict:
    return {"version": pkg_version("claude-pilot")}


@app.get("/api/projects")
def get_projects() -> list[dict]:
    return list_projects(_config.projects_dir)  # type: ignore[return-value]


@app.get("/api/projects/{project}/git-diff")
def git_diff(project: str) -> dict:
    import subprocess
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
    import subprocess
    path = _get_project_path(project)
    result = subprocess.run(
        ["claude", "-p", prompt],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=300,
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
    """Fetch a GitHub PR via gh CLI and ask Claude to review it."""
    import subprocess
    import json as _json
    path = _get_project_path(project)

    meta_result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "title,body"],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if meta_result.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail=meta_result.stderr.strip() or f"PR #{pr_number} not found",
        )

    meta = _json.loads(meta_result.stdout)
    title = meta.get("title", "")
    body = (meta.get("body") or "").strip()

    diff_result = subprocess.run(
        ["gh", "pr", "diff", str(pr_number)],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    diff = diff_result.stdout

    prompt = (
        f"Review PR #{pr_number}: {title}\n\n"
        + (f"Description:\n{body}\n\n" if body else "")
        + f"Diff:\n{diff}\n\n"
        + "Provide:\n"
        + "1. A brief summary of the functional changes\n"
        + "2. Critical issues and bugs only (skip minor style nits)\n\n"
        + "Be concise."
    )

    result = subprocess.run(
        ["claude", "-p", prompt],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return {
        "review": result.stdout.strip(),
        "error": result.stderr.strip(),
        "returncode": result.returncode,
    }


@app.post("/api/projects/{project}/git-pull")
def git_pull(project: str) -> dict:
    import subprocess
    path = _get_project_path(project)
    result = subprocess.run(
        ["git", "pull"],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


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
    name: str = Body(..., embed=True),
    yolo: bool = Body(False, embed=True),
) -> dict:
    path = _get_project_path(project)
    return session_mgr.start_session(
        project=project,
        project_path=path,
        name=name,
        prefix=_config.tmux_session_prefix,
        db_path=str(_config.db_path),
        yolo=yolo,
    )


@app.get("/api/sessions/{project}/pane")
def get_pane(project: str) -> dict:
    """Return raw tmux pane output — useful for diagnosing startup failures."""
    from pilot.sessions import _tmux_session_name, _capture_pane, _session_exists
    session_name = _tmux_session_name(_config.tmux_session_prefix, project)
    if not _session_exists(session_name):
        return {"exists": False, "output": None}
    return {"exists": True, "output": _capture_pane(session_name)}


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


@app.post("/api/sessions/{project}/history/{session_id}/resume")
def resume_session(
    project: str,
    session_id: int,
    name: str = Body("", embed=True),
    yolo: bool = Body(False, embed=True),
) -> dict:
    """Start a new session that resumes the conversation from a prior session."""
    path = _get_project_path(project)
    record = pilot_db.get_session_by_id(str(_config.db_path), session_id)
    if not record or record["project"] != project:
        raise HTTPException(status_code=404, detail="Session not found")
    resume_name = name.strip() or f"cont. {record.get('name') or record['started_at'][:16]}"
    return session_mgr.resume_session(
        session_id=session_id,
        project=project,
        project_path=path,
        name=resume_name,
        prefix=_config.tmux_session_prefix,
        db_path=str(_config.db_path),
        yolo=yolo,
    )


@app.post("/api/sessions/{project}/send")
def send_to_session(
    project: str,
    text: str = Body(..., embed=True),
) -> dict:
    """Send text to the most recently started active RC session via tmux send-keys."""
    _get_project_path(project)  # 404 if unknown project
    text = text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must not be empty")
    sent = session_mgr.send_keys(project, _config.tmux_session_prefix, str(_config.db_path), text)
    if not sent:
        raise HTTPException(status_code=409, detail="No active session for this project")
    return {"ok": True}


@app.get("/api/sessions/{project}")
def get_sessions(project: str) -> dict:
    """Return all live running sessions for *project*."""
    _get_project_path(project)  # 404 if unknown project
    sessions = session_mgr.list_running_sessions(
        project=project,
        prefix=_config.tmux_session_prefix,
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
    logger.info("starting claude-pilot on {}:{}", _config.host, _config.port)
    logger.info("projects_dir={} db={} log={}", _config.projects_dir, _config.db_path, log_file)
    uvicorn.run(
        "pilot.main:app",
        host=_config.host,
        port=_config.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
