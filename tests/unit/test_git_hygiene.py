"""Focused tests for git_hygiene.py."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import logging
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


def test_delete_merged_branch_is_removed(tmp_path: Path) -> None:
    """A fully-merged agent branch is deleted with ``branch -d``."""
    with patch(
        "bernstein.core.git_hygiene.run_git",
        side_effect=[
            # `branch --list agent/*`
            GitResult(0, "  agent/merged\n", ""),
            # `merge-base --is-ancestor agent/merged main` -> success
            GitResult(0, "", ""),
            # `branch -d agent/merged`
            GitResult(0, "", ""),
        ],
    ) as mock_run_git:
        deleted, skipped = _delete_merged_agent_branches(tmp_path)

    assert deleted == 1
    assert skipped == 0
    # Safe delete (-d), NOT force delete (-D).
    assert mock_run_git.call_args_list[2].args[0] == ["branch", "-d", "agent/merged"]


def test_delete_unmerged_branch_is_preserved(tmp_path: Path, caplog: object) -> None:
    """An unmerged agent branch is NOT deleted and a warning is logged."""
    assert hasattr(caplog, "at_level")  # satisfy type checker

    with (
        caplog.at_level(logging.WARNING, logger="bernstein.core.git.git_hygiene"),  # type: ignore[attr-defined]
        patch(
            "bernstein.core.git_hygiene.run_git",
            side_effect=[
                # `branch --list agent/*`
                GitResult(0, "* agent/unmerged\n", ""),
                # `merge-base --is-ancestor` -> non-zero (not an ancestor)
                GitResult(1, "", ""),
            ],
        ) as mock_run_git,
    ):
        deleted, skipped = _delete_merged_agent_branches(tmp_path)

    assert deleted == 0
    assert skipped == 1
    # Only two git calls: list + ancestry probe. No branch deletion happened.
    assert len(mock_run_git.call_args_list) == 2
    delete_calls = [
        call for call in mock_run_git.call_args_list if call.args[0][:1] == ["branch"] and "-D" in call.args[0]
    ]
    assert delete_calls == []
    # Warning message mentions the branch and preservation.
    messages = [r.getMessage() for r in caplog.records]  # type: ignore[attr-defined]
    assert any("agent/unmerged" in m and "Preserving" in m for m in messages)


def test_delete_active_session_branch_is_preserved(tmp_path: Path) -> None:
    """Branches owned by live agents are never touched, even if merged."""
    with patch(
        "bernstein.core.git_hygiene.run_git",
        side_effect=[
            GitResult(0, "agent/alive-session\n", ""),
        ],
    ) as mock_run_git:
        deleted, skipped = _delete_merged_agent_branches(tmp_path, active_session_ids={"alive-session"})

    assert deleted == 0
    assert skipped == 1
    # Only the list call — no ancestry check, no deletion.
    assert len(mock_run_git.call_args_list) == 1


def test_force_unmerged_true_deletes_unmerged_branch(tmp_path: Path) -> None:
    """The privileged ``force_unmerged`` opt-in uses ``branch -D``."""
    with patch(
        "bernstein.core.git_hygiene.run_git",
        side_effect=[
            GitResult(0, "agent/unmerged\n", ""),
            # ancestry probe -> not merged
            GitResult(1, "", ""),
            # forced delete
            GitResult(0, "", ""),
        ],
    ) as mock_run_git:
        deleted, skipped = _delete_merged_agent_branches(tmp_path, force_unmerged=True)

    assert deleted == 1
    assert skipped == 0
    assert mock_run_git.call_args_list[2].args[0] == ["branch", "-D", "agent/unmerged"]


def test_delete_mix_of_branches(tmp_path: Path) -> None:
    """Mixed list: merged branch deleted, unmerged preserved, active preserved."""
    with patch(
        "bernstein.core.git_hygiene.run_git",
        side_effect=[
            GitResult(0, "agent/merged\nagent/unmerged\nagent/live\n", ""),
            # ancestry of agent/merged -> ok
            GitResult(0, "", ""),
            # delete agent/merged -> ok
            GitResult(0, "", ""),
            # ancestry of agent/unmerged -> not ancestor
            GitResult(1, "", ""),
            # agent/live is skipped BEFORE any git call because its session id
            # is in active_session_ids.
        ],
    ) as mock_run_git:
        deleted, skipped = _delete_merged_agent_branches(tmp_path, active_session_ids={"live"})

    assert deleted == 1
    assert skipped == 2
    # Confirm only four git invocations: list + ancestry + delete + ancestry.
    assert len(mock_run_git.call_args_list) == 4


def test_non_agent_branches_are_ignored(tmp_path: Path) -> None:
    """Lines that aren't agent/* branches are skipped entirely."""
    with patch(
        "bernstein.core.git_hygiene.run_git",
        side_effect=[
            GitResult(0, "main\nfeature/x\n", ""),
        ],
    ) as mock_run_git:
        deleted, skipped = _delete_merged_agent_branches(tmp_path)

    assert deleted == 0
    assert skipped == 0
    # Only the initial list call — we never touch non-agent branches.
    assert len(mock_run_git.call_args_list) == 1


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
        patch("bernstein.core.git.git_hygiene._delete_merged_agent_branches", return_value=(3, 1)),
        patch("bernstein.core.git.git_hygiene._drop_stale_stashes", return_value=1),
        patch("bernstein.core.git.git_hygiene._clean_stale_runtime") as mock_runtime,
        patch("bernstein.core.git.git_hygiene.run_git", return_value=GitResult(0, "", "")),
    ):
        stats = run_hygiene(tmp_path, full=True)

    assert stats == {
        "worktrees_cleaned": 2,
        "branches_deleted": 3,
        "branches_skipped": 1,
        "stash_dropped": 1,
    }
    mock_runtime.assert_called_once_with(tmp_path)


def test_run_hygiene_forwards_active_sessions_and_force_flag(tmp_path: Path) -> None:
    """run_hygiene threads active_session_ids and force_unmerged to the helper."""
    with (
        patch("bernstein.core.git.git_hygiene._clean_stale_worktrees", return_value=0),
        patch(
            "bernstein.core.git.git_hygiene._delete_merged_agent_branches",
            return_value=(0, 0),
        ) as mock_delete,
        patch("bernstein.core.git.git_hygiene.run_git", return_value=GitResult(0, "", "")),
    ):
        run_hygiene(
            tmp_path,
            active_session_ids={"abc123"},
            force_unmerged=True,
            target_branch="develop",
        )

    mock_delete.assert_called_once()
    _args, kwargs = mock_delete.call_args
    assert kwargs["target_branch"] == "develop"
    assert kwargs["force_unmerged"] is True
    assert "abc123" in kwargs["active_session_ids"]
