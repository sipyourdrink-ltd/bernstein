"""Tests for WorktreeManager — create/cleanup lifecycle (mocked subprocess)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from bernstein.core.worktree import WorktreeError, WorktreeManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(stdout: str = "", stderr: str = "") -> MagicMock:
    """Return a mock CompletedProcess with returncode=0."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = stderr
    return m


def _fail(stderr: str = "git error", stdout: str = "") -> MagicMock:
    """Return a mock CompletedProcess with returncode=1."""
    m = MagicMock()
    m.returncode = 1
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Temporary directory acting as a fake repo root."""
    return tmp_path


@pytest.fixture
def mgr(repo_root: Path) -> WorktreeManager:
    return WorktreeManager(repo_root)


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------

class TestCreate:
    def test_returns_worktree_path(self, mgr: WorktreeManager, repo_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            result = mgr.create("sess1")

        expected = repo_root / ".sdd/worktrees/sess1"
        assert result == expected

    def test_calls_git_worktree_add(self, mgr: WorktreeManager, repo_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            mgr.create("sess1")

        mock_run.assert_called_once_with(
            [
                "git", "worktree", "add",
                str(repo_root / ".sdd/worktrees/sess1"),
                "-b", "agent/sess1",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_creates_base_directory(self, mgr: WorktreeManager, repo_root: Path) -> None:
        with patch("subprocess.run", return_value=_ok()):
            mgr.create("sess1")

        assert (repo_root / ".sdd/worktrees").is_dir()

    def test_raises_if_worktree_path_exists(
        self, mgr: WorktreeManager, repo_root: Path
    ) -> None:
        worktree_path = repo_root / ".sdd/worktrees/sess1"
        worktree_path.mkdir(parents=True)

        with pytest.raises(WorktreeError, match="already exists"):
            mgr.create("sess1")

    def test_raises_on_git_failure(self, mgr: WorktreeManager) -> None:
        with patch("subprocess.run", return_value=_fail("some git error")):
            with pytest.raises(WorktreeError, match="git worktree add failed"):
                mgr.create("sess1")

    def test_raises_with_branch_already_exists_hint(self, mgr: WorktreeManager) -> None:
        with patch(
            "subprocess.run",
            return_value=_fail("fatal: 'agent/sess1' already exists"),
        ):
            with pytest.raises(WorktreeError, match="already exists"):
                mgr.create("sess1")


# ---------------------------------------------------------------------------
# cleanup()
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_calls_worktree_remove_and_branch_delete(
        self, mgr: WorktreeManager, repo_root: Path
    ) -> None:
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            mgr.cleanup("sess1")

        expected_calls = [
            call(
                [
                    "git", "worktree", "remove", "--force",
                    str(repo_root / ".sdd/worktrees/sess1"),
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=30,
            ),
            call(
                ["git", "branch", "-D", "agent/sess1"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            ),
        ]
        mock_run.assert_has_calls(expected_calls)

    def test_does_not_raise_on_worktree_remove_failure(
        self, mgr: WorktreeManager
    ) -> None:
        """cleanup() is best-effort — individual git failures should not propagate."""
        with patch("subprocess.run", return_value=_fail("no worktree")):
            # Should complete without raising
            mgr.cleanup("sess1")

    def test_does_not_raise_on_branch_delete_failure(
        self, mgr: WorktreeManager
    ) -> None:
        responses = [_ok(), _fail("branch not found")]
        with patch("subprocess.run", side_effect=responses):
            mgr.cleanup("sess1")

    def test_does_not_raise_on_subprocess_exception(
        self, mgr: WorktreeManager
    ) -> None:
        with patch("subprocess.run", side_effect=OSError("git not found")):
            mgr.cleanup("sess1")


# ---------------------------------------------------------------------------
# list_active()
# ---------------------------------------------------------------------------

class TestListActive:
    def _porcelain(self, repo_root: Path, session_ids: list[str]) -> str:
        """Build a fake ``git worktree list --porcelain`` output."""
        lines: list[str] = []
        # Include the main worktree which should be ignored
        lines.append(f"worktree {repo_root}")
        lines.append("HEAD abc1234")
        lines.append("branch refs/heads/master")
        lines.append("")
        for sid in session_ids:
            wt = repo_root / ".sdd/worktrees" / sid
            lines.append(f"worktree {wt}")
            lines.append("HEAD def5678")
            lines.append(f"branch refs/heads/agent/{sid}")
            lines.append("")
        return "\n".join(lines)

    def test_returns_empty_when_no_worktrees(self, mgr: WorktreeManager, repo_root: Path) -> None:
        output = self._porcelain(repo_root, [])
        with patch("subprocess.run", return_value=_ok(stdout=output)):
            assert mgr.list_active() == []

    def test_returns_session_ids(self, mgr: WorktreeManager, repo_root: Path) -> None:
        output = self._porcelain(repo_root, ["sessA", "sessB"])
        with patch("subprocess.run", return_value=_ok(stdout=output)):
            result = mgr.list_active()

        assert set(result) == {"sessA", "sessB"}

    def test_ignores_non_agent_worktrees(self, mgr: WorktreeManager, repo_root: Path) -> None:
        # Worktree outside .sdd/worktrees — should be ignored
        extra = f"worktree /some/other/path\nHEAD aaa\nbranch refs/heads/other\n"
        base = self._porcelain(repo_root, ["s1"])
        output = base + "\n" + extra
        with patch("subprocess.run", return_value=_ok(stdout=output)):
            result = mgr.list_active()

        assert result == ["s1"]

    def test_returns_empty_on_git_error(self, mgr: WorktreeManager) -> None:
        with patch("subprocess.run", return_value=_fail("git error")):
            assert mgr.list_active() == []

    def test_returns_empty_on_subprocess_exception(self, mgr: WorktreeManager) -> None:
        with patch("subprocess.run", side_effect=OSError("git not found")):
            assert mgr.list_active() == []


# ---------------------------------------------------------------------------
# Round-trip: create → cleanup → list_active
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_create_then_cleanup_then_list_empty(
        self, mgr: WorktreeManager, repo_root: Path
    ) -> None:
        """Verify create returns a path and cleanup succeeds without error."""
        with patch("subprocess.run", return_value=_ok()) as mock_run:
            path = mgr.create("trip1")
            assert path == repo_root / ".sdd/worktrees/trip1"
            mgr.cleanup("trip1")

        # 3 subprocess calls: worktree add, worktree remove, branch -D
        assert mock_run.call_count == 3
