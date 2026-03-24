"""
pi-lot FastAPI application.

Routes (all sync — FastAPI runs them in a thread pool):
  GET    /                                  → serve index.html
  GET    /api/projects                      → list projects
  POST   /api/sessions/{project}            → start session, return {url, status}
  GET    /api/sessions/{project}            → get session status + RC URL if running
  DELETE /api/sessions/{project}            → kill session
  GET    /api/sessions/{project}/history    → list past sessions for project
  POST   /api/sessions/{project}/resume     → re-launch a stopped session (Phase 2: +context)
  GET    /api/logs?since=<cursor>           → recent log lines for browser console forwarding
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

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


app = FastAPI(title="pi-lot", version="0.1.0", lifespan=lifespan)

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

@app.get("/api/projects")
def get_projects() -> list[dict]:
    return list_projects(_config.projects_dir)  # type: ignore[return-value]


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
def start_session(project: str) -> dict:
    path = _get_project_path(project)
    return session_mgr.start_session(
        project=project,
        project_path=path,
        prefix=_config.tmux_session_prefix,
        db_path=str(_config.db_path),
    )


@app.get("/api/sessions/{project}/history")
def get_history(project: str) -> list[dict]:
    """Return all past session records for *project*, newest first."""
    _get_project_path(project)  # 404 if unknown project
    return pilot_db.list_sessions(str(_config.db_path), project)


@app.post("/api/sessions/{project}/resume")
def resume_session(project: str) -> dict:
    """
    Re-launch a session for *project*.
    Phase 1: identical to start — just restarts RC.
    Phase 2 will inject last-session context here.
    """
    path = _get_project_path(project)
    logger.info("resume requested for project={}", project)
    return session_mgr.start_session(
        project=project,
        project_path=path,
        prefix=_config.tmux_session_prefix,
        db_path=str(_config.db_path),
    )


@app.get("/api/sessions/{project}")
def get_session(project: str) -> dict:
    return session_mgr.get_session_status(
        project=project,
        prefix=_config.tmux_session_prefix,
        db_path=str(_config.db_path),
    )


@app.delete("/api/sessions/{project}")
def delete_session(project: str) -> dict:
    return session_mgr.kill_session(
        project=project,
        prefix=_config.tmux_session_prefix,
        db_path=str(_config.db_path),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    log_file = _config.db_path.parent / "pilot.log"
    logger.add(log_file, rotation="10 MB", retention=3, level="DEBUG")
    logger.info("starting pi-lot on {}:{}", _config.host, _config.port)
    logger.info("projects_dir={} db={} log={}", _config.projects_dir, _config.db_path, log_file)
    uvicorn.run(
        "pilot.main:app",
        host=_config.host,
        port=_config.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
