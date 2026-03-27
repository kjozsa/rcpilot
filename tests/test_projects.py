"""Tests for pilot.projects — project discovery."""

from __future__ import annotations

from pathlib import Path

from pilot.projects import list_projects


def test_empty_dir_returns_empty_list(tmp_path: Path) -> None:
    result = list_projects(tmp_path)
    assert result == []


def test_nonexistent_dir_returns_empty_list(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    result = list_projects(missing)
    assert result == []


def test_discovers_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    # A regular file should not appear
    (tmp_path / "readme.txt").write_text("hi")

    result = list_projects(tmp_path)
    names = [p["name"] for p in result]

    assert "alpha" in names
    assert "beta" in names
    assert "readme.txt" not in names


def test_hidden_dirs_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "visible").mkdir()
    (tmp_path / ".hidden").mkdir()

    result = list_projects(tmp_path)
    names = [p["name"] for p in result]

    assert "visible" in names
    assert ".hidden" not in names


def test_has_git_true_when_dot_git_present(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    (project_dir / ".git").mkdir()

    result = list_projects(tmp_path)
    assert len(result) == 1
    assert result[0]["has_git"] is True


def test_has_git_false_when_no_dot_git(tmp_path: Path) -> None:
    (tmp_path / "plain-dir").mkdir()

    result = list_projects(tmp_path)
    assert result[0]["has_git"] is False


def test_result_is_sorted_alphabetically(tmp_path: Path) -> None:
    for name in ["zebra", "apple", "mango"]:
        (tmp_path / name).mkdir()

    result = list_projects(tmp_path, sort_by="alpha")
    names = [p["name"] for p in result]

    assert names == sorted(names)


def test_path_field_is_absolute(tmp_path: Path) -> None:
    (tmp_path / "proj").mkdir()

    result = list_projects(tmp_path)
    assert Path(result[0]["path"]).is_absolute()
