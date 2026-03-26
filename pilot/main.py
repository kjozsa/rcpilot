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
    db_path = str(_config.db_path)
    logger.info("initialising database at {}", db_path)
    _config.db_path.parent.mkdir(parents=True, exist_ok=True)
    await pilot_db.init_db(db_path)
    _ensure_code_review_plugin()

    logger.info("starting watchdog")
    _thread, _stop = start_watchdog(_config)

    yield  # server runs here

    logger.info("shutting down — signalling watchdog")
    _stop.set()
    _thread.join(timeout=5)


app = FastAPI(title="rcpilot", version="0.1.0", lifespan=lifespan)

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
    return {"version": pkg_version("rcpilot")}


@app.get("/api/projects")
def get_projects(sort_by: str = "modified") -> list[dict]:
    return list_projects(_config.projects_dir, sort_by=sort_by)  # type: ignore[return-value]


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
    """Kick off a PR code review in the background.

    The code-review plugin posts its findings as a GitHub PR comment, so we
    launch the claude process detached and return immediately rather than
    waiting up to several minutes for it to finish.
    """
    import subprocess
    path = _get_project_path(project)
    subprocess.Popen(
        ["claude", "-p", f"/code-review:code-review {pr_number}"],
        cwd=path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "review": f"Review started for PR #{pr_number}. Results will be posted as a comment on the GitHub PR.",
        "error": "",
        "returncode": 0,
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
        db_path=str(_config.db_path),
        yolo=yolo,
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
    uvicorn.run(
        "pilot.main:app",
        host=_config.host,
        port=_config.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
