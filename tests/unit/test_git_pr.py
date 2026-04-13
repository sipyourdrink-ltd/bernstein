"""Unit tests for git branch, merge, and PR helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import bernstein.core.git_pr as git_pr
import pytest
from bernstein.core.git_basic import GitResult


def test_merge_with_conflict_detection_aborts_and_reports_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def _fake_run_git(args: list[str], cwd: Path, timeout: int = 30, **kwargs: object) -> GitResult:
        calls.append(args)
        if args[:3] == ["merge", "--no-commit", "--no-ff"]:
            return GitResult(returncode=1, stdout="", stderr="merge failed")
        if args[:2] == ["status", "--porcelain"]:
            return GitResult(returncode=0, stdout="UU src/demo.py\n", stderr="")
        if args[:2] == ["merge", "--abort"]:
            return GitResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(git_pr, "run_git", _fake_run_git)

    result = git_pr.merge_with_conflict_detection(tmp_path, "feature/demo")

    assert result.success is False
    assert result.conflicting_files == ["src/demo.py"]
    assert ["merge", "--abort"] in calls


def test_create_task_branch_delegates_to_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def _fake_run_git(args: list[str], cwd: Path, timeout: int = 30, **kwargs: object) -> GitResult:
        seen.extend(args)
        return GitResult(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(git_pr, "run_git", _fake_run_git)

    result = git_pr.create_task_branch(tmp_path, "bernstein/task-1")

    assert result.ok is True
    assert seen == ["checkout", "-b", "bernstein/task-1"]


def test_create_github_pr_returns_success_and_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _success_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="https://example/pr/1\n", stderr="")

    def _error_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="bad auth")

    monkeypatch.setattr(git_pr.subprocess, "run", _success_run)
    success = git_pr.create_github_pr(tmp_path, title="PR", body="Body", head="feature/demo")
    monkeypatch.setattr(git_pr.subprocess, "run", _error_run)
    failure = git_pr.create_github_pr(tmp_path, title="PR", body="Body", head="feature/demo")

    assert success.success is True
    assert success.pr_url == "https://example/pr/1"
    assert failure.success is False
    assert failure.error == "bad auth"


def test_merge_with_conflict_detection_returns_non_conflict_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _fake_run_git(args: list[str], cwd: Path, timeout: int = 30, **kwargs: object) -> GitResult:
        if args[:3] == ["merge", "--no-commit", "--no-ff"]:
            return GitResult(returncode=1, stdout="", stderr="branch not found")
        if args[:2] == ["status", "--porcelain"]:
            return GitResult(returncode=0, stdout="", stderr="")
        if args[:2] == ["merge", "--abort"]:
            return GitResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(git_pr, "run_git", _fake_run_git)

    result = git_pr.merge_with_conflict_detection(tmp_path, "feature/missing")

    assert result.success is False
    assert result.conflicting_files == []
    assert result.error == "branch not found"


def test_create_github_pr_handles_subprocess_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise_timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="gh pr create", timeout=30)

    monkeypatch.setattr(git_pr.subprocess, "run", _raise_timeout)

    result = git_pr.create_github_pr(tmp_path, title="PR", body="Body", head="feature/demo")

    assert result.success is False
    assert "timed out" in result.error.lower()
