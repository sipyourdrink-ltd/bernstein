"""Focused tests for basic git helpers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.git_basic import GitResult, run_git, safe_push, stage_all_except, stage_task_files


def test_run_git_raises_called_process_error_when_check_is_true(tmp_path: Path) -> None:
    """run_git raises CalledProcessError on non-zero exit when check=True."""
    completed = subprocess.CompletedProcess(args=["git"], returncode=1, stdout="", stderr="boom")

    with (
        patch("bernstein.core.git.git_basic.subprocess.run", return_value=completed),
        pytest.raises(subprocess.CalledProcessError),
    ):
        run_git(["status"], tmp_path, check=True)


def test_stage_task_files_adds_untracked_siblings_and_filters_runtime_artifacts(tmp_path: Path) -> None:
    """stage_task_files stages owned files plus sibling untracked files, but never runtime artifacts."""
    with (
        patch(
            "bernstein.core.git_basic.status_porcelain",
            return_value="?? src/new_helper.py\n?? .sdd/runtime/server.log\n?? docs/readme.md\n",
        ),
        patch("bernstein.core.git.git_basic.run_git") as mock_run_git,
    ):
        staged = stage_task_files(tmp_path, ["src/main.py"])

    assert staged == ["src/main.py", "src/new_helper.py"]
    mock_run_git.assert_called_once_with(["add", "--", "src/main.py", "src/new_helper.py"], tmp_path)


def test_stage_all_except_unstages_explicit_and_never_stage_paths(tmp_path: Path) -> None:
    """stage_all_except bulk-adds first, then resets explicit exclusions and protected runtime dirs."""
    with patch("bernstein.core.git.git_basic.run_git") as mock_run_git:
        stage_all_except(tmp_path, exclude=["README.md"])

    assert mock_run_git.call_args_list[0].args[0] == ["add", "-A"]
    reset_args = mock_run_git.call_args_list[1].args[0]
    assert reset_args[:3] == ["reset", "HEAD", "--"]
    assert "README.md" in reset_args
    assert ".sdd/runtime/" in reset_args
    assert ".sdd/metrics/" in reset_args


def test_safe_push_corrects_master_and_rebases_when_remote_is_ahead(tmp_path: Path) -> None:
    """safe_push rewrites master to main and rebases before pushing when behind the remote."""
    with (
        patch("bernstein.core.git.git_basic.fetch", return_value=GitResult(0, "", "")),
        patch(
            "bernstein.core.git_basic.run_git",
            side_effect=[
                GitResult(0, "2", ""),  # rev-list count
                GitResult(0, "", ""),  # rebase
                GitResult(0, "pushed", ""),  # push
            ],
        ) as mock_run_git,
    ):
        result = safe_push(tmp_path, "master")

    assert result.ok is True
    assert mock_run_git.call_args_list[0].args[0] == ["rev-list", "--count", "HEAD..origin/main"]
    assert mock_run_git.call_args_list[1].args[0] == ["rebase", "origin/main"]
    assert mock_run_git.call_args_list[2].args[0] == ["push", "origin", "main"]
