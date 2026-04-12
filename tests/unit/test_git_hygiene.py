"""Focused tests for git_hygiene.py."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bernstein.core.git_basic import GitResult
from bernstein.core.git_hygiene import (
    _clean_stale_runtime,
    _clean_stale_worktrees,
    _delete_merged_agent_branches,
    _drop_stale_stashes,
    run_hygiene,
)


def test_clean_stale_worktrees_removes_only_untracked_dirs(tmp_path: Path) -> None:
    """_clean_stale_worktrees removes stale directories not present in git worktree list."""
    worktrees_dir = tmp_path / ".sdd" / "worktrees"
    tracked = worktrees_dir / "tracked"
    stale = worktrees_dir / "stale"
    tracked.mkdir(parents=True)
    stale.mkdir(parents=True)

    with patch(
        "bernstein.core.git_hygiene.run_git",
        return_value=GitResult(0, f"worktree {tracked}\n", ""),
    ):
        cleaned = _clean_stale_worktrees(tmp_path)

    assert cleaned == 1
    assert tracked.exists()
    assert not stale.exists()


def test_delete_merged_agent_branches_deletes_only_agent_branches(tmp_path: Path) -> None:
    """_delete_merged_agent_branches deletes every listed agent/* branch."""
    with patch(
        "bernstein.core.git_hygiene.run_git",
        side_effect=[
            GitResult(0, "  agent/a\n* agent/b\n", ""),
            GitResult(0, "", ""),
            GitResult(0, "", ""),
        ],
    ) as mock_run_git:
        deleted = _delete_merged_agent_branches(tmp_path)

    assert deleted == 2
    assert mock_run_git.call_args_list[1].args[0] == ["branch", "-D", "agent/a"]
    assert mock_run_git.call_args_list[2].args[0] == ["branch", "-D", "agent/b"]


def test_drop_stale_stashes_counts_entries_and_clears_when_present(tmp_path: Path) -> None:
    """_drop_stale_stashes returns stash count and clears the stash list."""
    with patch(
        "bernstein.core.git_hygiene.run_git",
        side_effect=[GitResult(0, "stash@{0}: WIP\nstash@{1}: WIP\n", ""), GitResult(0, "", "")],
    ) as mock_run_git:
        dropped = _drop_stale_stashes(tmp_path)

    assert dropped == 2
    assert mock_run_git.call_args_list[1].args[0] == ["stash", "clear"]


def test_clean_stale_runtime_removes_pid_files_and_agents_json(tmp_path: Path) -> None:
    """_clean_stale_runtime clears stale pid files and persisted agents state."""
    runtime = tmp_path / ".sdd" / "runtime" / "pids"
    runtime.mkdir(parents=True)
    (runtime / "A.pid").write_text("123", encoding="utf-8")
    agents_json = tmp_path / ".sdd" / "runtime" / "agents.json"
    agents_json.write_text("{}", encoding="utf-8")

    _clean_stale_runtime(tmp_path)

    assert not (runtime / "A.pid").exists()
    assert not agents_json.exists()


def test_run_hygiene_full_accumulates_counts_and_runs_full_cleanup(tmp_path: Path) -> None:
    """run_hygiene(full=True) aggregates helper counts and performs runtime cleanup."""
    with (
        patch("bernstein.core.git.git_hygiene._clean_stale_worktrees", return_value=2),
        patch("bernstein.core.git.git_hygiene._delete_merged_agent_branches", return_value=3),
        patch("bernstein.core.git.git_hygiene._drop_stale_stashes", return_value=1),
        patch("bernstein.core.git.git_hygiene._clean_stale_runtime") as mock_runtime,
        patch("bernstein.core.git.git_hygiene.run_git", return_value=GitResult(0, "", "")),
    ):
        stats = run_hygiene(tmp_path, full=True)

    assert stats == {"worktrees_cleaned": 2, "branches_deleted": 3, "stash_dropped": 1}
    mock_runtime.assert_called_once_with(tmp_path)
