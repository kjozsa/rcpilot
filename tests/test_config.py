"""Tests for pilot.config — loading, defaults, env-var override."""

from __future__ import annotations

from pathlib import Path

import pytest

from pilot.config import Config, load_config


def test_defaults_when_no_file(tmp_path: Path) -> None:
    """load_config returns sensible defaults when the config file is absent."""
    missing = tmp_path / "nonexistent.toml"
    cfg = load_config(missing)

    assert isinstance(cfg, Config)
    assert cfg.port == 8000
    assert cfg.host == "0.0.0.0"
    assert cfg.tmux_session_prefix == "pilot-"


def test_loads_values_from_toml(tmp_path: Path) -> None:
    """Values written in a TOML file are reflected in the Config object."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'projects_dir = "/tmp/my-projects"\n'
        'host = "127.0.0.1"\n'
        "port = 9000\n"
        'tmux_session_prefix = "myprefix-"\n'
    )

    cfg = load_config(config_file)

    assert cfg.projects_dir == Path("/tmp/my-projects")
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 9000
    assert cfg.tmux_session_prefix == "myprefix-"


def test_partial_toml_keeps_other_defaults(tmp_path: Path) -> None:
    """A TOML that only sets some keys leaves the rest at their defaults."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("port = 7777\n")

    cfg = load_config(config_file)

    assert cfg.port == 7777
    assert cfg.host == "0.0.0.0"  # default untouched


def test_env_var_path_used_when_no_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PILOT_CONFIG env var is respected when no path is passed explicitly."""
    config_file = tmp_path / "via_env.toml"
    config_file.write_text('tmux_session_prefix = "env-"\n')

    monkeypatch.setenv("PILOT_CONFIG", str(config_file))
    cfg = load_config()  # no path argument

    assert cfg.tmux_session_prefix == "env-"


def test_tilde_expansion_in_projects_dir(tmp_path: Path) -> None:
    """projects_dir with a ~ is expanded to an absolute path."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('projects_dir = "~/code"\n')

    cfg = load_config(config_file)

    assert not str(cfg.projects_dir).startswith("~")
    assert cfg.projects_dir.is_absolute()
