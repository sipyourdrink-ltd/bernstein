"""Safety guards for GitHub backlog sync, goal precedence, and default-branch merges.

Covers three independent bootstrap/merge hazards:

A. GitHub backlog auto-sync must be opt-in (default off) so it cannot silently
   pull every open issue into the backlog and displace a seeded goal.
B. When a seeded goal is dropped because the backlog is non-empty, the operator
   must be warned LOUDLY (the precedence used to be silent).
C. Agent worktree merges must refuse to land on the repository's default
   (protected) branch unless the operator explicitly opts in.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from bernstein.core.config.seed_config import (
    GithubConfig,
    github_backlog_sync_enabled,
)

# ---------------------------------------------------------------------------
# Problem A: GitHub backlog auto-sync is opt-in (default off)
# ---------------------------------------------------------------------------


def _seed(*, sync_backlog: bool = False, goal: str = "Ship the parser") -> SimpleNamespace:
    return SimpleNamespace(goal=goal, github=GithubConfig(sync_backlog=sync_backlog))


def test_github_sync_disabled_by_default() -> None:
    assert github_backlog_sync_enabled(_seed(), env={}) is False


def test_github_sync_enabled_when_config_true() -> None:
    assert github_backlog_sync_enabled(_seed(sync_backlog=True), env={}) is True


def test_github_sync_env_override_enables() -> None:
    assert github_backlog_sync_enabled(_seed(), env={"BERNSTEIN_SYNC_GITHUB_BACKLOG": "1"}) is True


def test_github_sync_env_override_disables_config() -> None:
    assert (
        github_backlog_sync_enabled(
            _seed(sync_backlog=True),
            env={"BERNSTEIN_SYNC_GITHUB_BACKLOG": "false"},
        )
        is False
    )


def test_maybe_sync_not_called_when_disabled() -> None:
    """Default (off): sync_github_issues_to_backlog is never invoked."""
    from bernstein.core.orchestration import bootstrap

    sync_mock = MagicMock(return_value=3)
    with patch("bernstein.core.github.sync_github_issues_to_backlog", sync_mock):
        result = bootstrap._maybe_sync_github_backlog(_seed(sync_backlog=False), Path("/x"))

    assert result == 0
    sync_mock.assert_not_called()


def test_maybe_sync_called_when_enabled() -> None:
    """Opt-in (on): sync_github_issues_to_backlog runs and returns its count."""
    from bernstein.core.orchestration import bootstrap

    sync_mock = MagicMock(return_value=4)
    with patch("bernstein.core.github.sync_github_issues_to_backlog", sync_mock):
        result = bootstrap._maybe_sync_github_backlog(_seed(sync_backlog=True), Path("/x"))

    assert result == 4
    sync_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Problem B: loud warning when a seeded goal is shadowed by a non-empty backlog
# ---------------------------------------------------------------------------


def test_warns_when_goal_shadowed_by_backlog() -> None:
    from bernstein.core.orchestration import bootstrap

    fake_console = MagicMock()
    with patch("bernstein.core.orchestration.bootstrap.console", fake_console):
        bootstrap._warn_if_goal_shadowed_by_backlog(
            _seed(goal="Do the thing"),
            backlog_count=5,
            prior_session=None,
            gh_synced=0,
        )

    printed = " ".join(str(c.args[0]) for c in fake_console.print.call_args_list if c.args)
    assert "WARNING" in printed
    assert "seeded goal is being ignored" in printed
    assert "precedence" in printed
    assert "BERNSTEIN_TASK_FILTER" in printed


def test_no_warning_when_backlog_empty() -> None:
    from bernstein.core.orchestration import bootstrap

    fake_console = MagicMock()
    with patch("bernstein.core.orchestration.bootstrap.console", fake_console):
        bootstrap._warn_if_goal_shadowed_by_backlog(
            _seed(goal="Do the thing"),
            backlog_count=0,
            prior_session=None,
            gh_synced=0,
        )

    fake_console.print.assert_not_called()


def test_no_warning_when_prior_session_present() -> None:
    from bernstein.core.orchestration import bootstrap

    fake_console = MagicMock()
    with patch("bernstein.core.orchestration.bootstrap.console", fake_console):
        bootstrap._warn_if_goal_shadowed_by_backlog(
            _seed(goal="Do the thing"),
            backlog_count=5,
            prior_session=SimpleNamespace(completed_task_ids=[]),
            gh_synced=0,
        )

    fake_console.print.assert_not_called()


def test_warning_notes_auto_synced_backlog() -> None:
    from bernstein.core.orchestration import bootstrap

    fake_console = MagicMock()
    with patch("bernstein.core.orchestration.bootstrap.console", fake_console):
        bootstrap._warn_if_goal_shadowed_by_backlog(
            _seed(goal="Do the thing"),
            backlog_count=5,
            prior_session=None,
            gh_synced=5,
        )

    printed = " ".join(str(c.args[0]) for c in fake_console.print.call_args_list if c.args)
    assert "auto-synced" in printed
    assert "github.sync_backlog" in printed


# ---------------------------------------------------------------------------
# Problem C: refuse merge/push onto the repository default branch
# ---------------------------------------------------------------------------


def _session(session_id: str = "s1", task_ids: list[str] | None = None) -> Any:
    return SimpleNamespace(id=session_id, task_ids=task_ids or ["t1"], task_title="")


def _patched_collector():
    """Patch metric collector so record_merge_result is a no-op we can assert on."""
    collector = MagicMock()
    return patch(
        "bernstein.core.metric_collector.get_collector",
        return_value=collector,
    ), collector


def test_merge_refused_on_default_branch(tmp_path: Path) -> None:
    from bernstein.core.agents import spawner_merge

    merge_fn = MagicMock()
    coll_patch, collector = _patched_collector()
    with (
        patch("bernstein.core.git_ops.current_branch", return_value="main"),
        patch("bernstein.core.git_ops.resolve_default_branch", return_value="main"),
        patch("bernstein.core.git_ops.safe_push") as safe_push_mock,
        patch.object(spawner_merge, "_allow_merge_to_default_branch", return_value=False),
        coll_patch,
    ):
        result = spawner_merge._run_merge_and_push(_session(), tmp_path, merge_fn)

    # Merge and push never happen when the target is the default branch.
    merge_fn.assert_not_called()
    safe_push_mock.assert_not_called()
    assert result is not None
    assert result.success is False
    assert "default branch" in (result.error or "")
    collector.record_merge_result.assert_called_with("t1", success=False)
    # The refusal is recorded to a visible journal.
    assert (tmp_path / ".sdd" / "runtime" / "refused_merges.jsonl").exists()


def test_merge_proceeds_on_non_default_branch(tmp_path: Path) -> None:
    from bernstein.core.git_ops import GitResult, MergeResult

    from bernstein.core.agents import spawner_merge

    merge_fn = MagicMock(return_value=MergeResult(success=True, conflicting_files=[]))
    push_ok = GitResult(returncode=0, stdout="", stderr="")
    coll_patch, _collector = _patched_collector()
    with (
        patch("bernstein.core.git_ops.current_branch", return_value="feat/x"),
        patch("bernstein.core.git_ops.resolve_default_branch", return_value="main"),
        patch("bernstein.core.git_ops.safe_push", return_value=push_ok) as safe_push_mock,
        coll_patch,
    ):
        result = spawner_merge._run_merge_and_push(_session(), tmp_path, merge_fn)

    merge_fn.assert_called_once()
    assert result is not None and result.success is True
    # Pushes the branch actually merged into, never a hard-coded "main".
    safe_push_mock.assert_called_once_with(tmp_path, "feat/x")


def test_merge_allowed_on_default_branch_with_override(tmp_path: Path) -> None:
    from bernstein.core.git_ops import GitResult, MergeResult

    from bernstein.core.agents import spawner_merge

    merge_fn = MagicMock(return_value=MergeResult(success=True, conflicting_files=[]))
    push_ok = GitResult(returncode=0, stdout="", stderr="")
    coll_patch, _collector = _patched_collector()
    with (
        patch("bernstein.core.git_ops.current_branch", return_value="main"),
        patch("bernstein.core.git_ops.resolve_default_branch", return_value="main"),
        patch("bernstein.core.git_ops.safe_push", return_value=push_ok) as safe_push_mock,
        patch.object(spawner_merge, "_allow_merge_to_default_branch", return_value=True),
        coll_patch,
    ):
        result = spawner_merge._run_merge_and_push(_session(), tmp_path, merge_fn)

    merge_fn.assert_called_once()
    assert result is not None and result.success is True
    safe_push_mock.assert_called_once_with(tmp_path, "main")
