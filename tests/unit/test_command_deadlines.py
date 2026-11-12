"""Deadlines and failure outcomes for short operator commands."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import errno
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.policy_cmd import check_policies
from bernstein.cli.commands.undo_cmd import undo_cmd
from bernstein.core.autofix import review_router
from bernstein.core.persistence import ledger_git
from bernstein.core.persistence.workspace import RepoConfig, Workspace
from bernstein.core.sandbox.ssh_backend import SandboxConnectionError, SSHSandboxBackend
from bernstein.core.tasks.context_extractors import extract_co_change_neighbours, extract_test_to_source_map


def _timeout(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    timeout = kwargs["timeout"]
    assert timeout in (30, 120)
    raise subprocess.TimeoutExpired(args, timeout)


def test_policy_cli_reports_unavailable_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with (
        patch("bernstein.cli.commands.policy_cmd.load_policy_engine") as load,
        patch("bernstein.core.security.policy_engine.subprocess.run", side_effect=_timeout),
    ):
        result = CliRunner().invoke(check_policies)
    assert result.exit_code == 1
    assert "Timed out reading the repository diff" in result.output
    load.return_value.check.assert_not_called()


@pytest.mark.parametrize("extract", [extract_co_change_neighbours, extract_test_to_source_map])
def test_context_extractors_fall_back_after_timeout(tmp_path: Path, extract: Any) -> None:
    with patch("bernstein.core.tasks.context_extractors.subprocess.check_output", side_effect=_timeout):
        assert extract(tmp_path, ["src/file.py"]) == {"src/file.py": []}


@pytest.mark.parametrize(
    ("command", "invoke"),
    [
        ("cat-file", lambda path: ledger_git._read_blob(path, "blob")),
        ("hash-object", lambda path: ledger_git._hash_blob(path, b"test")),
        ("mktree", lambda path: ledger_git._mktree(path, [])),
        ("commit-tree", lambda path: ledger_git._commit_tree(path, "tree", parent=None, message="anchor")),
    ],
)
def test_ledger_git_timeout_reports_failed_plumbing(tmp_path: Path, command: str, invoke: Any) -> None:
    with (
        patch("bernstein.core.persistence.ledger_git.subprocess.run", side_effect=_timeout),
        pytest.raises(ledger_git.LedgerGitError, match=f"git {command} timed out"),
    ):
        invoke(tmp_path)


def test_workspace_timeout_discards_partial_clone(tmp_path: Path) -> None:
    repo = RepoConfig(name="example", path=Path("example"), url="https://example.invalid/repo.git")
    workspace = Workspace(root=tmp_path, repos=[repo])

    def partial_clone(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        path = Path(args[-1])
        path.mkdir()
        (path / "partial").touch()
        return _timeout(args, **kwargs)

    with patch("bernstein.core.persistence.workspace.subprocess.run", side_effect=partial_clone):
        assert workspace.clone_missing() == []
    assert list(tmp_path.iterdir()) == []


def test_workspace_status_keeps_existing_fallbacks_after_timeout(tmp_path: Path) -> None:
    with patch("bernstein.core.persistence.workspace.subprocess.run", side_effect=_timeout):
        assert Workspace._git_current_branch(tmp_path) == "unknown"
        assert not Workspace._git_is_clean(tmp_path)
        assert Workspace._git_ahead_behind(tmp_path) == (0, 0)


def test_review_router_handles_gh_and_git_timeouts(tmp_path: Path) -> None:
    with patch("bernstein.core.autofix.review_router.subprocess.run", side_effect=_timeout):
        with pytest.raises(review_router.GhInvocationError, match="gh timed out"):
            review_router._default_gh_runner(["gh", "pr", "view"])
        assert review_router.resolve_pr_number(environ={}, workdir=tmp_path) is None


def test_ssh_open_reports_timeout_and_close_still_cleans_up(tmp_path: Path) -> None:
    sock = tmp_path / "control.sock"
    with (
        patch.object(SSHSandboxBackend, "_build_control_socket_path", return_value=sock),
        patch("bernstein.core.sandbox.ssh_backend.subprocess.run", side_effect=_timeout),
    ):
        backend = SSHSandboxBackend(host="example.invalid", path="/repo")
        with pytest.raises(SandboxConnectionError, match="control connection timed out"):
            backend.ensure_control_master()
        sock.touch()
        backend.close()
    assert not sock.exists()


def test_undo_git_log_timeout_is_not_an_empty_result() -> None:
    with patch("bernstein.cli.commands.undo_cmd.subprocess.run", side_effect=_timeout):
        result = CliRunner().invoke(undo_cmd, ["example", "--yes"])

    assert result.exit_code == 1
    assert "Timed out reading Git history for undo." in result.output
    assert "No matching commits" not in result.output


@pytest.mark.parametrize("first_completes", [False, True])
def test_undo_aborts_and_audits_uncertain_revert(first_completes: bool) -> None:
    first = MagicMock(returncode=0)
    first.communicate.return_value = (b"", b"")
    failed = MagicMock(pid=12345)
    failed.communicate.side_effect = subprocess.TimeoutExpired("git revert", 120)
    first.__enter__.return_value = first
    failed.__enter__.return_value = failed

    with (
        patch("bernstein.cli.commands.undo_cmd.subprocess.run") as run,
        patch("bernstein.cli.commands.undo_cmd.subprocess.Popen") as popen,
        patch("bernstein.cli.commands.undo_cmd.kill_process_group", return_value=True) as kill_group,
        patch("bernstein.core.tasks.lifecycle.get_audit_log") as get_audit,
        patch("bernstein.cli.commands.undo_cmd._run_post_revert_tests") as verify,
    ):
        run.return_value = subprocess.CompletedProcess(
            ["git", "log"], 0, stdout="first task:example\nsecond task:example\nthird task:example"
        )
        popen.side_effect = [first, failed] if first_completes else [failed]
        result = CliRunner().invoke(undo_cmd, ["example", "--yes"])

    confirmed = int(first_completes)
    uncertain = "second" if first_completes else "first"
    assert result.exit_code == 1
    assert f"Revert of {uncertain} timed out" in result.output
    assert "Check git status and recent commits" in result.output
    assert run.call_args.kwargs["timeout"] == 30
    assert popen.call_count == confirmed + 1
    failed.communicate.assert_called_once_with(timeout=120)
    kill_group.assert_called_once_with(failed.pid, sig=9)
    get_audit.return_value.log.assert_called_once()
    details = get_audit.return_value.log.call_args.kwargs["details"]
    assert details["commit_count"] == confirmed
    assert details["outcome"] == "unknown"
    assert details["uncertain_commit"] == uncertain
    verify.assert_not_called()


def test_workspace_competing_clones_publish_one_checkout(tmp_path: Path) -> None:
    repo = RepoConfig(name="example", path=Path("example"), url="https://example.invalid/repo.git")
    workspace = Workspace(root=tmp_path, repos=[repo])
    ready = Barrier(2)

    def clone(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        checkout = Path(args[-1])
        checkout.mkdir()
        (checkout / "ready").touch()
        ready.wait(timeout=5)
        return subprocess.CompletedProcess(args, 0)

    with (
        patch("bernstein.core.persistence.workspace.subprocess.run", side_effect=clone),
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = sorted(workers.map(lambda _: workspace.clone_missing(), range(2)))

    assert results == [[], ["example"]]
    assert (tmp_path / "example" / "ready").is_file()
    assert list(tmp_path.iterdir()) == [tmp_path / "example"]


def test_workspace_preserves_empty_destination_created_during_clone(tmp_path: Path) -> None:
    destination = tmp_path / "example"
    repo = RepoConfig(name="example", path=Path("example"), url="https://example.invalid/repo.git")
    workspace = Workspace(root=tmp_path, repos=[repo])
    inode = None

    def clone(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        nonlocal inode
        Path(args[-1]).mkdir()
        destination.mkdir()
        inode = destination.stat().st_ino
        return subprocess.CompletedProcess(args, 0)

    with patch("bernstein.core.persistence.workspace.subprocess.run", side_effect=clone):
        assert workspace.clone_missing() == []

    assert inode is not None
    assert destination.stat().st_ino == inode
    assert list(destination.iterdir()) == []


def test_workspace_can_retry_after_publish_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    repo = RepoConfig(name="example", path=Path("example"), url="https://example.invalid/repo.git")
    workspace = Workspace(root=tmp_path, repos=[repo])

    def clone(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        Path(args[-1]).mkdir()
        return subprocess.CompletedProcess(args, 0)

    with patch("bernstein.core.persistence.workspace.subprocess.run", side_effect=clone):
        with patch(
            "bernstein.core.persistence.workspace._publish_clone",
            side_effect=OSError(errno.ENOTSUP, "Exclusive directory rename is unavailable"),
        ):
            assert workspace.clone_missing() == []
        assert list(tmp_path.iterdir()) == []
        assert workspace.clone_missing() == ["example"]

    assert "Could not publish clone 'example'" in caplog.text
    assert list(tmp_path.iterdir()) == [tmp_path / "example"]
