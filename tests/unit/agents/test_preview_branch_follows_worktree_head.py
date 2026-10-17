"""The merge preview grades the branch the session's worktree actually has checked out.

A resume session continues a replaced agent's preserved worktree. When the
hand-over rename does not happen, the worktree keeps the replaced agent's
branch, and a preview of ``agent/<resume-session>`` fails with "not something
we can merge" on every attempt even though the commits are there.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bernstein.core.tasks.task_lifecycle import _agent_preview_branch


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "run@example.com")
    _git(root, "config", "user.name", "Run")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "seed")
    return root


def _orch(repo: Path, paths: dict[str, Path]) -> SimpleNamespace:
    return SimpleNamespace(_workdir=repo, _spawner=SimpleNamespace(get_worktree_path=paths.get))


def test_resume_session_on_replaced_agents_branch(repo: Path) -> None:
    wt = repo / ".sdd" / "worktrees" / "backend-017c3451"
    _git(repo, "worktree", "add", "-b", "agent/backend-017c3451", str(wt))
    orch = _orch(repo, {"backend-resume-fd6308cb": wt})

    assert _agent_preview_branch(orch, "backend-resume-fd6308cb") == "agent/backend-017c3451"


def test_adopted_worktree_keeps_session_branch(repo: Path) -> None:
    wt = repo / ".sdd" / "worktrees" / "backend-resume-1"
    _git(repo, "worktree", "add", "-b", "agent/backend-resume-1", str(wt))
    orch = _orch(repo, {"backend-resume-1": wt})

    assert _agent_preview_branch(orch, "backend-resume-1") == "agent/backend-resume-1"


def test_worktree_gone_falls_back_to_session_branch(repo: Path) -> None:
    orch = _orch(repo, {"qa-1": repo / ".sdd" / "worktrees" / "qa-1"})

    assert _agent_preview_branch(orch, "qa-1") == "agent/qa-1"


def test_session_working_in_run_checkout_has_nothing_to_preview(repo: Path) -> None:
    orch = _orch(repo, {"solo-1": repo})

    assert _agent_preview_branch(orch, "solo-1") is None
