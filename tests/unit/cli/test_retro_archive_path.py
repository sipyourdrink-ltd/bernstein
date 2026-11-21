"""``bernstein retro --archive`` refuses a path that names no archive file (#7959).

An explicit missing file and a directory both exited 0 with "No tasks found in the archive." and
wrote no report, exactly as an empty archive does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import retro

ROW = {"task_id": "t1", "title": "plant", "role": "backend", "status": "done", "completed_at": 1_700_000_000.0}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _retro(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(retro, list(args))
    return result.exit_code, result.output


def test_an_explicit_missing_archive_fails_and_names_the_path(workspace: Path) -> None:
    code, out = _retro("--archive", "missing.jsonl", "-o", "report.md")
    assert code != 0
    assert "missing.jsonl" in out
    assert not (workspace / "report.md").exists()


def test_a_directory_as_the_archive_fails_and_names_the_path(workspace: Path) -> None:
    (workspace / "archive-dir").mkdir()
    code, out = _retro("--archive", "archive-dir", "-o", "report.md")
    assert code != 0
    assert "archive-dir" in out


def test_an_existing_empty_archive_is_an_empty_result(workspace: Path) -> None:
    (workspace / "empty.jsonl").write_text("")
    code, out = _retro("--archive", "empty.jsonl", "-o", "report.md")
    assert code == 0
    assert "No tasks found in the archive" in out


def test_no_archive_in_an_empty_workspace_is_an_empty_result(workspace: Path) -> None:
    code, out = _retro("-o", "report.md")
    assert code == 0
    assert "No tasks found in the archive" in out


def test_a_valid_archive_writes_a_report(workspace: Path) -> None:
    (workspace / "tasks.jsonl").write_text(json.dumps(ROW) + "\n")
    code, out = _retro("--archive", "tasks.jsonl", "-o", "report.md")
    assert code == 0, out
    assert (workspace / "report.md").is_file()
