"""A resumed agent's work is found under the resumed session's own name.

Crash recovery and gate repair resume a task in the worktree of the agent
they replace. The merge, the merge gates and cleanup find a session's work
as ``.sdd/worktrees/<session_id>`` on ``agent/<session_id>``; a resume
session that kept the old worktree name was gated against the repository
root and merged from a branch that did not exist, so its work never landed.
``WorktreeManager.adopt`` hands the worktree to the new session's names and
``spawn_for_resume`` applies it right before the agent starts.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.spawner import AgentSpawner
from bernstein.core.worktree import WorktreeError, WorktreeManager

from bernstein.adapters.base import CLIAdapter, SpawnError, SpawnResult
from bernstein.core.agents.spawner_merge import merge_worktree_branch

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "work")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


def _agent_worktree(manager: WorktreeManager, session_id: str) -> Path:
    """A worktree with one committed and one uncommitted file, as an agent leaves it."""
    path = manager.create(session_id)
    (path / "done.txt").write_text("committed\n", encoding="utf-8")
    _git(path, "add", "done.txt")
    _git(path, "commit", "-q", "-m", "agent work")
    (path / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    return path


def _make_task() -> Task:
    return Task(
        id="T-001",
        title="Implement feature X",
        description="Write the code.",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus("open"),
        task_type=TaskType.STANDARD,
    )


def _mock_adapter() -> MagicMock:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=42, proc=None, log_path=None)
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    adapter.name.return_value = "mock"
    return adapter


class TestAdopt:
    def test_moves_worktree_renames_branch_and_keeps_all_work(self, repo: Path) -> None:
        manager = WorktreeManager(repo)
        old = _agent_worktree(manager, "backend-aaa")

        new = manager.adopt(old, "backend-resume-bbb")

        assert new == repo.resolve() / ".sdd" / "worktrees" / "backend-resume-bbb"
        assert not old.exists()
        assert (new / "done.txt").read_text(encoding="utf-8") == "committed\n"
        assert (new / "wip.txt").read_text(encoding="utf-8") == "uncommitted\n"
        assert _git(new, "rev-parse", "--abbrev-ref", "HEAD") == "agent/backend-resume-bbb"
        assert _git(repo, "branch", "--list", "agent/backend-aaa") == ""
        locks = repo / ".sdd" / "worktrees" / ".locks"
        assert (locks / "backend-resume-bbb.lock").exists()
        assert not (locks / "backend-aaa.lock").exists()

    def test_same_name_is_a_no_op(self, repo: Path) -> None:
        manager = WorktreeManager(repo)
        path = _agent_worktree(manager, "backend-aaa")

        assert manager.adopt(path, "backend-aaa") == path
        assert _git(path, "rev-parse", "--abbrev-ref", "HEAD") == "agent/backend-aaa"

    def test_refuses_an_existing_target(self, repo: Path) -> None:
        manager = WorktreeManager(repo)
        old = _agent_worktree(manager, "backend-aaa")
        manager.create("backend-bbb")

        with pytest.raises(WorktreeError, match="already exists"):
            manager.adopt(old, "backend-bbb")
        assert old.exists()

    def test_refuses_a_path_outside_the_base(self, repo: Path, tmp_path: Path) -> None:
        manager = WorktreeManager(repo)
        stray = tmp_path / "elsewhere" / "backend-aaa"
        stray.mkdir(parents=True)

        with pytest.raises(WorktreeError, match="outside the worktree base"):
            manager.adopt(stray, "backend-resume-bbb")

    def test_failed_branch_rename_moves_the_worktree_back(self, repo: Path) -> None:
        manager = WorktreeManager(repo)
        old = _agent_worktree(manager, "backend-aaa")
        _git(repo, "branch", "agent/backend-resume-bbb")  # rename target already taken

        with pytest.raises(WorktreeError, match="branch -m"):
            manager.adopt(old, "backend-resume-bbb")

        assert (old / "wip.txt").exists()
        assert not (repo / ".sdd" / "worktrees" / "backend-resume-bbb").exists()
        assert _git(old, "rev-parse", "--abbrev-ref", "HEAD") == "agent/backend-aaa"

    def test_owns_only_linked_worktrees_in_its_slot_directory(self, repo: Path, tmp_path: Path) -> None:
        manager = WorktreeManager(repo)
        real = manager.create("backend-aaa")
        plain = repo / ".sdd" / "worktrees" / "not-a-worktree"
        plain.mkdir()

        assert manager.owns(real)
        assert not manager.owns(plain)
        assert not manager.owns(tmp_path)


class TestSpawnForResume:
    def _spawner(self, repo: Path, adapter: MagicMock) -> AgentSpawner:
        return AgentSpawner(
            adapter=adapter,
            templates_dir=repo.parent / "templates",
            workdir=repo,
            default_model="mock-model",
        )

    def test_resumed_work_lands_under_the_resume_session_and_merges(self, repo: Path) -> None:
        adapter = _mock_adapter()
        spawner = self._spawner(repo, adapter)
        assert spawner._worktree_mgr is not None
        preserved = _agent_worktree(spawner._worktree_mgr, "backend-aaa")

        session = spawner.spawn_for_resume([_make_task()], worktree_path=preserved, changed_files=["done.txt"])

        expected = repo.resolve() / ".sdd" / "worktrees" / session.id
        assert adapter.spawn.call_args.kwargs["workdir"] == expected
        assert spawner.get_worktree_path(session.id) == expected
        # The merge finds the resumed session's branch and lands the work.
        result = merge_worktree_branch(session.id, repo)
        assert result.success, result.error
        assert (repo / "done.txt").read_text(encoding="utf-8") == "committed\n"

    def test_agent_that_does_not_start_hands_the_worktree_back(self, repo: Path) -> None:
        adapter = _mock_adapter()
        adapter.spawn.side_effect = SpawnError("adapter refused")
        spawner = self._spawner(repo, adapter)
        assert spawner._worktree_mgr is not None
        preserved = _agent_worktree(spawner._worktree_mgr, "backend-aaa")

        with pytest.raises(SpawnError, match="adapter refused"):
            spawner.spawn_for_resume([_make_task()], worktree_path=preserved, changed_files=[])

        # The next resume attempt still finds the worktree where it was preserved.
        assert (preserved / "wip.txt").exists()
        assert _git(preserved, "rev-parse", "--abbrev-ref", "HEAD") == "agent/backend-aaa"
