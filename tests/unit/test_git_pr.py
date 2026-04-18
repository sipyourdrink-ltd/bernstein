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


def _stub_bisect_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured_argv: list[list[str]],
    bisect_stdout: str = "",
) -> None:
    """Wire ``run_git`` + ``subprocess.run`` stubs shared across bisect tests."""

    def _fake_run_git(args: list[str], cwd: Path, timeout: int = 30, **kwargs: object) -> GitResult:
        return GitResult(returncode=0, stdout="", stderr="")

    def _fake_run(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_argv.append(list(cmd))
        # Pretend `git check-ref-format --branch X` passes for non-malicious input.
        if len(cmd) >= 2 and cmd[:2] == ["git", "check-ref-format"]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=bisect_stdout, stderr="")

    monkeypatch.setattr(git_pr, "run_git", _fake_run_git)
    monkeypatch.setattr(git_pr.subprocess, "run", _fake_run)


def test_bisect_regression_parses_quoted_test_cmd_with_shlex(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[list[str]] = []
    _stub_bisect_env(
        monkeypatch,
        captured_argv=captured,
        bisect_stdout="abcdef1234567 is the first bad commit\n",
    )

    sha = git_pr.bisect_regression(
        tmp_path,
        test_cmd='pytest -k "name with spaces" tests/unit',
        good_ref="main",
        bad_ref="HEAD",
    )

    bisect_calls = [c for c in captured if c[:3] == ["git", "bisect", "run"]]
    assert bisect_calls, "expected a git bisect run invocation"
    # shlex preserves the quoted expression as a single argv element.
    assert bisect_calls[0] == [
        "git",
        "bisect",
        "run",
        "pytest",
        "-k",
        "name with spaces",
        "tests/unit",
    ]
    assert sha == "abcdef1234567"


def test_bisect_regression_rejects_leading_flag_injection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[list[str]] = []
    _stub_bisect_env(monkeypatch, captured_argv=captured)

    with pytest.raises(ValueError, match="must not start with a flag"):
        git_pr.bisect_regression(
            tmp_path,
            test_cmd="--log-file=/tmp/x pytest",
        )
    assert not any(c[:3] == ["git", "bisect", "run"] for c in captured)


def test_bisect_regression_rejects_malformed_shlex(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[list[str]] = []
    _stub_bisect_env(monkeypatch, captured_argv=captured)

    with pytest.raises(ValueError, match="failed to parse test_cmd"):
        git_pr.bisect_regression(tmp_path, test_cmd='pytest "unterminated')


def test_bisect_regression_rejects_bad_ref_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _fake_run_git(args: list[str], cwd: Path, timeout: int = 30, **kwargs: object) -> GitResult:
        return GitResult(returncode=0, stdout="", stderr="")

    def _fake_run(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["git", "check-ref-format"]:
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="bad ref")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_pr, "run_git", _fake_run_git)
    monkeypatch.setattr(git_pr.subprocess, "run", _fake_run)

    with pytest.raises(ValueError, match="invalid git ref"):
        git_pr.bisect_regression(tmp_path, test_cmd="pytest", good_ref="not a ref")

    # Leading-dash refs must be rejected before any git process is invoked.
    with pytest.raises(ValueError, match="must not start with '-'"):
        git_pr.bisect_regression(tmp_path, test_cmd="pytest", good_ref="--upload-pack=x")


def test_bisect_regression_accepts_test_argv_over_string(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[list[str]] = []
    _stub_bisect_env(
        monkeypatch,
        captured_argv=captured,
        bisect_stdout="1234567 is the first bad commit\n",
    )

    sha = git_pr.bisect_regression(
        tmp_path,
        test_argv=["pytest", "-x", "tests/unit/test_git_pr.py"],
    )

    bisect_calls = [c for c in captured if c[:3] == ["git", "bisect", "run"]]
    assert bisect_calls[0][3:] == ["pytest", "-x", "tests/unit/test_git_pr.py"]
    assert sha == "1234567"
