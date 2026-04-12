"""Tests for automated conflict resolution and merge strategy."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.git_ops import GitResult
from bernstein.core.git_pr import (
    MergeResult,
    _parse_conflict_files,
    merge_with_conflict_detection,
)
from bernstein.core.spawner import AgentSpawner

REPO = Path("/fake/repo")


# ------------------------------------------------------------------
# MergeResult dataclass
# ------------------------------------------------------------------


class TestMergeResult:
    def test_successful_merge(self) -> None:
        r = MergeResult(success=True, conflicting_files=[], merge_diff="1 file changed")
        assert r.success
        assert r.conflicting_files == []
        assert r.merge_diff == "1 file changed"
        assert r.error == ""

    def test_conflict_merge(self) -> None:
        r = MergeResult(success=False, conflicting_files=["src/a.py", "src/b.py"])
        assert not r.success
        assert r.conflicting_files == ["src/a.py", "src/b.py"]

    def test_error_merge(self) -> None:
        r = MergeResult(success=False, conflicting_files=[], error="branch not found")
        assert not r.success
        assert r.error == "branch not found"

    def test_frozen(self) -> None:
        r = MergeResult(success=True, conflicting_files=[])
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]


# ------------------------------------------------------------------
# _parse_conflict_files
# ------------------------------------------------------------------


class TestParseConflictFiles:
    @patch("bernstein.core.git.git_pr.run_git")
    def test_unmerged_files(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(
            0,
            "UU src/a.py\nUU src/b.py\nM  src/c.py\n",
            "",
        )
        result = _parse_conflict_files(REPO)
        assert result == ["src/a.py", "src/b.py"]

    @patch("bernstein.core.git.git_pr.run_git")
    def test_all_unmerged_codes(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(
            0,
            "UU file1.py\nAA file2.py\nDD file3.py\nAU file4.py\nUA file5.py\nDU file6.py\nUD file7.py\n",
            "",
        )
        result = _parse_conflict_files(REPO)
        assert len(result) == 7

    @patch("bernstein.core.git.git_pr.run_git")
    def test_no_conflicts(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "M  src/a.py\nA  src/b.py\n", "")
        result = _parse_conflict_files(REPO)
        assert result == []

    @patch("bernstein.core.git.git_pr.run_git")
    def test_empty_status(self, mock: MagicMock) -> None:
        mock.return_value = GitResult(0, "", "")
        result = _parse_conflict_files(REPO)
        assert result == []


# ------------------------------------------------------------------
# merge_with_conflict_detection
# ------------------------------------------------------------------


class TestMergeWithConflictDetection:
    # ``merge_with_conflict_detection`` now goes straight to
    # ``git merge --no-commit --no-ff`` (no rebase-first fallback); its
    # clean-merge path issues 4 git calls: merge, syntax-check diff,
    # commit, diff --stat. These tests used to mock a 7-call sequence
    # from the old rebase-first implementation, so the stdout for
    # ``diff --stat`` landed at index 6 and was never reached.

    @patch("bernstein.core.git.git_pr.run_git")
    def test_clean_merge(self, mock: MagicMock) -> None:
        mock.side_effect = [
            GitResult(0, "", ""),  # merge --no-commit --no-ff
            GitResult(0, "", ""),  # _check_python_syntax: diff --cached --name-only
            GitResult(0, "", ""),  # commit -m <msg>
            GitResult(0, "1 file changed\n", ""),  # diff HEAD~1 --stat
        ]
        result = merge_with_conflict_detection(REPO, "agent/session-1")
        assert result.success
        assert result.conflicting_files == []
        assert "1 file changed" in result.merge_diff

    @patch("bernstein.core.git.git_pr.run_git")
    def test_clean_merge_custom_message(self, mock: MagicMock) -> None:
        mock.side_effect = [
            GitResult(0, "", ""),  # merge --no-commit --no-ff
            GitResult(0, "", ""),  # _check_python_syntax
            GitResult(0, "", ""),  # commit
            GitResult(0, "", ""),  # diff --stat
        ]
        merge_with_conflict_detection(REPO, "feature/x", message="Custom merge msg")
        # Verify the commit used the custom message (index 2: merge, syntax, commit)
        commit_call = mock.call_args_list[2]
        assert "Custom merge msg" in commit_call[0][0]

    @patch("bernstein.core.git.git_pr.run_git")
    def test_conflict_detected(self, mock: MagicMock) -> None:
        mock.side_effect = [
            GitResult(1, "", "CONFLICT (content): Merge conflict in src/a.py"),  # merge fails
            GitResult(0, "UU src/a.py\nUU src/b.py\n", ""),  # status (conflicts)
            GitResult(0, "", ""),  # merge --abort
        ]
        result = merge_with_conflict_detection(REPO, "agent/session-1")
        assert not result.success
        assert result.conflicting_files == ["src/a.py", "src/b.py"]
        # Verify merge was aborted (last call)
        abort_call = mock.call_args_list[-1]
        assert "--abort" in abort_call[0][0]

    @patch("bernstein.core.git.git_pr.run_git")
    def test_non_conflict_failure(self, mock: MagicMock) -> None:
        mock.side_effect = [
            GitResult(128, "", "fatal: 'agent/bad' is not a commit"),  # merge fails
            GitResult(0, "", ""),  # status (no conflicts)
            GitResult(0, "", ""),  # merge --abort
        ]
        result = merge_with_conflict_detection(REPO, "agent/bad")
        assert not result.success
        assert result.conflicting_files == []
        assert "not a commit" in result.error

    @patch("bernstein.core.git.git_pr.run_git")
    def test_nothing_to_commit_after_merge(self, mock: MagicMock) -> None:
        """Branches are identical — merge succeeds but nothing to commit."""
        mock.side_effect = [
            GitResult(0, "", ""),  # merge --no-commit --no-ff
            GitResult(0, "", ""),  # _check_python_syntax
            GitResult(1, "", "nothing to commit"),  # commit fails
            GitResult(0, "", ""),  # merge --abort (fallback)
        ]
        result = merge_with_conflict_detection(REPO, "agent/session-1")
        assert result.success
        assert result.conflicting_files == []


# ------------------------------------------------------------------
# Spawner conflict resolution integration
# ------------------------------------------------------------------


class TestSpawnerConflictResolution:
    def _make_spawner(self, tmp_path: Path, mock_adapter: MagicMock, *, use_worktrees: bool = True) -> AgentSpawner:
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        return AgentSpawner(
            mock_adapter,
            templates_dir,
            tmp_path,
            use_worktrees=use_worktrees,
        )

    def test_reap_returns_merge_result_on_success(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=100)
        spawner = self._make_spawner(tmp_path, adapter)

        # Simulate a spawned agent with worktree
        session = MagicMock()
        session.id = "backend-abc12345"
        session.pid = 100

        proc = MagicMock()
        spawner._procs[session.id] = proc
        spawner._worktree_paths[session.id] = tmp_path / ".sdd" / "worktrees" / session.id

        clean_result = MergeResult(success=True, conflicting_files=[], merge_diff="ok")
        with patch.object(spawner, "_merge_worktree_branch", return_value=clean_result):
            result = spawner.reap_completed_agent(session)

        assert result is not None
        assert result.success

    def test_reap_returns_merge_result_on_conflict(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=100)
        spawner = self._make_spawner(tmp_path, adapter)

        session = MagicMock()
        session.id = "backend-abc12345"
        session.pid = 100

        proc = MagicMock()
        spawner._procs[session.id] = proc
        spawner._worktree_paths[session.id] = tmp_path / ".sdd" / "worktrees" / session.id

        conflict_result = MergeResult(
            success=False,
            conflicting_files=["src/a.py"],
        )
        with patch.object(spawner, "_merge_worktree_branch", return_value=conflict_result):
            result = spawner.reap_completed_agent(session)

        assert result is not None
        assert not result.success
        assert result.conflicting_files == ["src/a.py"]

    def test_reap_returns_none_without_worktree(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=100)
        spawner = self._make_spawner(tmp_path, adapter, use_worktrees=False)

        session = MagicMock()
        session.id = "backend-abc12345"
        session.pid = 100

        proc = MagicMock()
        spawner._procs[session.id] = proc

        result = spawner.reap_completed_agent(session)
        assert result is None

    def test_reap_returns_none_when_no_proc(self, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=100)
        spawner = self._make_spawner(tmp_path, adapter)

        session = MagicMock()
        session.id = "backend-abc12345"

        result = spawner.reap_completed_agent(session)
        assert result is None

    @patch("bernstein.core.agents.spawner_merge.merge_with_conflict_detection")
    def test_merge_worktree_branch_delegates(self, mock_merge: MagicMock, tmp_path: Path, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory(pid=100)
        spawner = self._make_spawner(tmp_path, adapter)

        mock_merge.return_value = MergeResult(
            success=True,
            conflicting_files=[],
            merge_diff="ok",
        )
        result = spawner._merge_worktree_branch("backend-abc12345")
        mock_merge.assert_called_once_with(
            tmp_path,
            "agent/backend-abc12345",
            message="Merge agent/backend-abc12345",
        )
        assert result.success

    @patch("bernstein.core.agents.spawner_merge.merge_with_conflict_detection")
    def test_merge_worktree_branch_handles_exception(
        self, mock_merge: MagicMock, tmp_path: Path, mock_adapter_factory
    ) -> None:
        adapter = mock_adapter_factory(pid=100)
        spawner = self._make_spawner(tmp_path, adapter)

        mock_merge.side_effect = RuntimeError("git binary missing")
        result = spawner._merge_worktree_branch("backend-abc12345")
        assert not result.success
        assert "git binary missing" in result.error
