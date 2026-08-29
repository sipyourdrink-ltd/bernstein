from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.tasks.context_extractors import extract_co_change_neighbours


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        (
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            *args,
        ),
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _commit(repo: Path, message: str, *paths: str) -> None:
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(message, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def test_neighbours_come_from_the_commit_graph_not_the_directory(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _commit(tmp_path, "first change", "src/model.py", "schemas/model.json")
    _commit(tmp_path, "same directory only", "src/other.py")
    assert extract_co_change_neighbours(tmp_path, ["src/model.py"]) == {
        "src/model.py": ["schemas/model.json"],
    }


def test_the_ranking_is_stable_under_input_reordering(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _commit(tmp_path, "shared change again", "src/a.py", "config/shared.toml")
    _commit(tmp_path, "shared change", "src/a.py", "config/shared.toml")
    _commit(tmp_path, "other change", "src/b.py", "config/other.toml")
    assert extract_co_change_neighbours(tmp_path, ["src/a.py", "src/b.py"]) == extract_co_change_neighbours(
        tmp_path, ["src/b.py", "src/a.py"]
    )


def test_the_cap_is_reported_not_silently_applied(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _git(tmp_path, "init")
    _commit(tmp_path, "many co-changes", "src/a.py", "one.txt", "two.txt", "three.txt")
    with caplog.at_level("INFO"):
        result = extract_co_change_neighbours(tmp_path, ["src/a.py"], limit=2)
    assert len(result["src/a.py"]) == 2
    assert "co-change neighbours truncated for src/a.py: kept 2 of 3" in caplog.text


def test_a_repository_with_one_commit_yields_no_neighbours_and_no_error(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _commit(tmp_path, "only target", "src/a.py")
    assert extract_co_change_neighbours(tmp_path, ["src/a.py"]) == {"src/a.py": []}
