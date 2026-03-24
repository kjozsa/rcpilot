"""Integration smoke tests for FastAPI routes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _make_app(tmp_path: Path) -> Any:
    """Build an app instance pointing at tmp_path for projects + DB."""
    from pilot import main as main_mod
    from pilot.config import Config
    from pilot import db as pilot_db

    db_path = tmp_path / "test.db"
    asyncio.run(pilot_db.init_db(str(db_path)))

    cfg = Config(projects_dir=tmp_path / "projects", db_path=db_path)
    cfg.projects_dir.mkdir()

    main_mod._config = cfg
    return main_mod.app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = _make_app(tmp_path)
    # Use lifespan=False so we don't spawn the real DB init + watchdog in tests
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def client_with_project(tmp_path: Path) -> tuple[TestClient, str]:
    from pilot import main as main_mod
    app = _make_app(tmp_path)
    project_name = "test-project"
    (main_mod._config.projects_dir / project_name).mkdir()
    return TestClient(app, raise_server_exceptions=True), project_name


def test_index_returns_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_get_projects_empty(client: TestClient) -> None:
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_projects_lists_directory(client_with_project: tuple) -> None:
    client, name = client_with_project
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert name in names


def test_get_history_empty(client_with_project: tuple) -> None:
    client, name = client_with_project
    resp = client.get(f"/api/sessions/{name}/history")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_history_unknown_project_returns_404(client: TestClient) -> None:
    resp = client.get("/api/sessions/no-such-project/history")
    assert resp.status_code == 404


def test_get_session_status_stopped_when_no_tmux(client_with_project: tuple) -> None:
    client, name = client_with_project
    with patch("pilot.sessions._session_exists", return_value=False):
        resp = client.get(f"/api/sessions/{name}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"


def test_start_session_unknown_project_returns_404(client: TestClient) -> None:
    resp = client.post("/api/sessions/no-such-project")
    assert resp.status_code == 404


def test_start_session_calls_session_mgr(client_with_project: tuple) -> None:
    client, name = client_with_project
    fake_result = {"status": "running", "rc_url": "https://claude.ai/rc/test", "session_id": 1}
    with patch("pilot.sessions.start_session", return_value=fake_result) as mock:
        resp = client.post(f"/api/sessions/{name}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    mock.assert_called_once()


def test_resume_calls_start_session(client_with_project: tuple) -> None:
    client, name = client_with_project
    fake_result = {"status": "running", "rc_url": "https://claude.ai/rc/test", "session_id": 2}
    with patch("pilot.sessions.start_session", return_value=fake_result) as mock:
        resp = client.post(f"/api/sessions/{name}/resume")
    assert resp.status_code == 200
    mock.assert_called_once()


def test_delete_session_calls_kill(client_with_project: tuple) -> None:
    client, name = client_with_project
    fake_result = {"status": "stopped", "rc_url": None, "session_id": None}
    with patch("pilot.sessions.kill_session", return_value=fake_result) as mock:
        resp = client.delete(f"/api/sessions/{name}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"
    mock.assert_called_once()
