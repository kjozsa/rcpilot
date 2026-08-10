"""Tests for remote-host addressing, config parsing and shell quoting."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from pilot import hosts as host_mgr
from pilot import projects as project_mgr
from pilot.config import Config, RemoteHost, load_config


# ---------------------------------------------------------------------------
# Project keys
# ---------------------------------------------------------------------------

def test_local_key_round_trips_unchanged() -> None:
    """Local projects keep their bare name, so existing DB rows stay valid."""
    assert host_mgr.split_key("rcpilot") == ("", "rcpilot")
    assert host_mgr.make_key("", "rcpilot") == "rcpilot"


def test_remote_key_carries_host() -> None:
    assert host_mgr.make_key("stardust", "rcpilot") == "stardust:rcpilot"
    assert host_mgr.split_key("stardust:rcpilot") == ("stardust", "rcpilot")


def test_resolve_returns_configured_host() -> None:
    host = RemoteHost(name="stardust", ssh="me@stardust")
    config = Config(hosts=[host])
    assert host_mgr.resolve(config, "rcpilot") == (None, "rcpilot")
    assert host_mgr.resolve(config, "stardust:rcpilot") == (host, "rcpilot")


def test_resolve_rejects_unknown_host() -> None:
    with pytest.raises(KeyError):
        host_mgr.resolve(Config(), "nosuchbox:rcpilot")


# ---------------------------------------------------------------------------
# Shell quoting
# ---------------------------------------------------------------------------

def test_sh_path_keeps_tilde_expandable() -> None:
    """A quoted '~' would name a literal directory, breaking every remote path."""
    assert host_mgr.sh_path("~/projects") == "~/projects"
    assert host_mgr.sh_path("~") == "~"


def test_sh_path_quotes_spaces_after_tilde() -> None:
    assert host_mgr.sh_path("~/my projects") == "~/'my projects'"


def test_sh_path_quotes_absolute_paths() -> None:
    assert host_mgr.sh_path("/home/me/my projects") == "'/home/me/my projects'"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, body: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return load_config(path)


def test_config_parses_hosts(tmp_path: Path) -> None:
    config = _write(tmp_path, """
projects_dir = "~/projects"

[[hosts]]
name = "stardust"
ssh = "kjozsa@stardust"
projects_dir = "~/code"
""")
    assert config.hosts == [
        RemoteHost(name="stardust", ssh="kjozsa@stardust", projects_dirs=("~/code",))
    ]


def test_config_parses_multiple_project_dirs(tmp_path: Path) -> None:
    """A machine keeping repos under several roots needs one entry, not several."""
    config = _write(tmp_path, """
[[hosts]]
name = "stardust"
ssh = "stardust"
projects_dir = ["~/workspace/atr", "~/workspace/dai"]
""")
    assert config.hosts[0].projects_dirs == ("~/workspace/atr", "~/workspace/dai")
    # New projects land in the first one.
    assert config.hosts[0].projects_dir == "~/workspace/atr"


def test_remote_scan_covers_every_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    host = RemoteHost(
        name="stardust", ssh="stardust",
        projects_dirs=("~/workspace/atr", "~/workspace/dai"),
    )
    captured: dict = {}

    def run_script(h, script, timeout=20.0):
        captured["script"] = script
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(host_mgr, "run_script", run_script)
    project_mgr.list_projects_on_host(host, use_cache=False)

    assert "scan ~/workspace/atr" in captured["script"]
    assert "scan ~/workspace/dai" in captured["script"]


def test_concurrent_scans_share_one_ssh_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page load resolves every project at once; they must not each ssh out."""
    import threading

    host = RemoteHost(name="stardust", ssh="stardust")
    calls = []

    def run_script(h, script, timeout=20.0):
        calls.append(1)
        time.sleep(0.2)  # long enough for the others to pile up on the lock
        return subprocess.CompletedProcess([], 0, _SCAN_ROW + "\n", "")

    monkeypatch.setattr(host_mgr, "run_script", run_script)
    project_mgr.invalidate_host_cache()

    threads = [
        threading.Thread(target=lambda: project_mgr.list_projects_on_host(host))
        for _ in range(12)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1


def test_remote_scan_keeps_first_of_duplicate_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key can only point at one directory, so collisions resolve to the first."""
    host = RemoteHost(name="stardust", ssh="stardust", projects_dirs=("~/a", "~/b"))
    rows = "\n".join([
        "shared\t/home/me/a/shared\t0\t\t\t\t\t1",
        "shared\t/home/me/b/shared\t0\t\t\t\t\t2",
    ])
    monkeypatch.setattr(
        host_mgr, "run_script",
        lambda *a, **k: subprocess.CompletedProcess([], 0, rows + "\n", ""),
    )
    projects = project_mgr.list_projects_on_host(host, use_cache=False)
    assert [p["path"] for p in projects] == ["/home/me/a/shared"]


def test_config_without_hosts_is_empty(tmp_path: Path) -> None:
    assert _write(tmp_path, 'port = 8000\n').hosts == []


def test_config_skips_malformed_hosts(tmp_path: Path) -> None:
    """One bad host entry must not take down every other project."""
    config = _write(tmp_path, """
[[hosts]]
name = "nossh"

[[hosts]]
name = "bad:name"
ssh = "me@bad"

[[hosts]]
name = "stardust"
ssh = "me@stardust"

[[hosts]]
name = "stardust"
ssh = "me@duplicate"
""")
    assert [h.name for h in config.hosts] == ["stardust"]
    assert config.hosts[0].ssh == "me@stardust"


# ---------------------------------------------------------------------------
# Remote project scan
# ---------------------------------------------------------------------------

_SCAN_ROW = "rcpilot\t/home/me/projects/rcpilot\t1\tmain\ta1b2c3\t2026-08-10T09:00:00+02:00\t 2 files changed\t1754812800"


def test_remote_scan_parses_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    host = RemoteHost(name="stardust", ssh="me@stardust")
    monkeypatch.setattr(
        host_mgr, "run_script",
        lambda *a, **k: subprocess.CompletedProcess([], 0, _SCAN_ROW + "\n", ""),
    )
    project_mgr.invalidate_host_cache()

    projects = project_mgr.list_projects_on_host(host, use_cache=False)
    assert len(projects) == 1
    p = projects[0]
    assert p["name"] == "stardust:rcpilot"
    assert p["label"] == "rcpilot"
    assert p["host"] == "stardust"
    assert p["path"] == "/home/me/projects/rcpilot"
    assert p["has_git"] is True
    assert p["git_branch"] == "main"
    assert p["git_diff_stat"] == " 2 files changed"


def test_remote_scan_ignores_partial_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Login-shell noise or a truncated line must not become a phantom project."""
    host = RemoteHost(name="stardust", ssh="me@stardust")
    monkeypatch.setattr(
        host_mgr, "run_script",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, "Welcome to Ubuntu\n" + _SCAN_ROW + "\nbroken\trow\n", ""
        ),
    )
    project_mgr.invalidate_host_cache()

    assert [p["label"] for p in project_mgr.list_projects_on_host(host, use_cache=False)] == ["rcpilot"]


def test_list_all_projects_survives_unreachable_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead host costs you its projects, not the whole list."""
    (tmp_path / "projects" / "local-one").mkdir(parents=True)

    def boom(*args, **kwargs):
        raise host_mgr.HostUnreachable("no route to host")

    monkeypatch.setattr(host_mgr, "run_script", boom)
    project_mgr.invalidate_host_cache()

    config = Config(
        projects_dir=tmp_path / "projects",
        hosts=[RemoteHost(name="stardust", ssh="me@stardust")],
    )
    assert [p["name"] for p in project_mgr.list_all_projects(config)] == ["local-one"]
