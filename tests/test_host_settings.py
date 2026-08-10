"""Tests for managing remote hosts from the settings UI."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from pilot.config import RemoteHost, load_config


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "# rcpilot configuration\n"
        'projects_dir = "~/projects"\n'
        "port = 8000\n"
        "\n"
        "# keep this comment\n"
        'window_cron = "0 7 * * *"\n'
    )
    monkeypatch.setenv("PILOT_CONFIG", str(path))
    return path


def _write(hosts: list[RemoteHost]) -> None:
    from pilot.main import _write_hosts_toml
    _write_hosts_toml(hosts)


# ---------------------------------------------------------------------------
# config.toml writing
# ---------------------------------------------------------------------------

def test_writing_hosts_preserves_the_rest_of_the_config(config_file: Path) -> None:
    _write([RemoteHost(name="stardust", ssh="stardust", projects_dirs=("~/a", "~/b"))])

    raw = tomllib.loads(config_file.read_text())
    assert raw["projects_dir"] == "~/projects"
    assert raw["port"] == 8000
    assert raw["window_cron"] == "0 7 * * *"
    assert raw["hosts"] == [
        {"name": "stardust", "ssh": "stardust", "projects_dir": ["~/a", "~/b"]}
    ]
    assert "# keep this comment" in config_file.read_text()


def test_rewriting_replaces_rather_than_appends(config_file: Path) -> None:
    _write([RemoteHost(name="a", ssh="a"), RemoteHost(name="b", ssh="b")])
    _write([RemoteHost(name="b", ssh="b")])

    raw = tomllib.loads(config_file.read_text())
    assert [h["name"] for h in raw["hosts"]] == ["b"]


def test_removing_every_host_leaves_a_valid_config(config_file: Path) -> None:
    _write([RemoteHost(name="stardust", ssh="stardust")])
    _write([])

    raw = tomllib.loads(config_file.read_text())
    assert "hosts" not in raw
    assert raw["port"] == 8000


def test_scalar_keys_after_a_host_block_are_not_swallowed(config_file: Path) -> None:
    """A key written after [[hosts]] belongs to that table, and dropping the
    table must not silently delete unrelated settings."""
    config_file.write_text(
        'port = 8000\n\n[[hosts]]\nname = "old"\nssh = "old"\n\n[other]\nkeep = 1\n'
    )
    _write([RemoteHost(name="new", ssh="new")])

    raw = tomllib.loads(config_file.read_text())
    assert raw["port"] == 8000
    assert raw["other"] == {"keep": 1}
    assert [h["name"] for h in raw["hosts"]] == ["new"]


def test_written_hosts_round_trip_through_the_loader(config_file: Path) -> None:
    hosts = [RemoteHost(name="stardust", ssh="kjozsa@stardust", projects_dirs=("~/x", "~/y"))]
    _write(hosts)
    assert load_config(config_file).hosts == hosts


def test_quoting_survives_awkward_paths(config_file: Path) -> None:
    _write([RemoteHost(name="box", ssh="box", projects_dirs=('~/my "code"', "~/a\\b"))])
    assert load_config(config_file).hosts[0].projects_dirs == ('~/my "code"', "~/a\\b")


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

def test_host_label_defaults_to_the_machine_name() -> None:
    from pilot.main import _host_from_body
    host = _host_from_body({"ssh": "kjozsa@stardust"})
    assert (host.name, host.ssh) == ("stardust", "kjozsa@stardust")


def test_projects_dirs_accepts_a_comma_separated_string() -> None:
    from pilot.main import _host_from_body
    host = _host_from_body({"ssh": "box", "projects_dirs": "~/a, ~/b\n~/c"})
    assert host.projects_dirs == ("~/a", "~/b", "~/c")


def test_projects_dirs_defaults_when_left_blank() -> None:
    from pilot.main import _host_from_body
    assert _host_from_body({"ssh": "box"}).projects_dirs == ("~/projects",)


def test_hostname_is_required() -> None:
    from fastapi import HTTPException
    from pilot.main import _host_from_body
    with pytest.raises(HTTPException) as exc:
        _host_from_body({"ssh": "  "})
    assert exc.value.status_code == 422


def test_host_name_rejects_the_key_separator() -> None:
    from fastapi import HTTPException
    from pilot.main import _host_from_body
    with pytest.raises(HTTPException) as exc:
        _host_from_body({"ssh": "box", "name": "we:ird"})
    assert "':'" in exc.value.detail
