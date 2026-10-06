"""Tests for the usage-window ticker."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from pilot import ticker
from pilot.config import Config


def test_fire_finds_claude_under_the_service_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # systemd's stripped PATH: claude in ~/.local/bin is not on it.
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    captured: dict = {}

    def run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ticker.subprocess, "run", run)

    ticker._fire(Config(projects_dir=tmp_path, port=9999), datetime(2026, 10, 6, 7, 0))

    path = captured["env"]["PATH"].split(":")
    assert str(Path.home() / ".local" / "bin") in path
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999/proxy"
    assert ticker.get_ticker_state()["fire_log"][0]["ok"] is True
