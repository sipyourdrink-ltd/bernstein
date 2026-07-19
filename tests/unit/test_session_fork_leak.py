"""Regression tests: ``fork_session`` must not leak a fork worktree or branch
when a post-creation step fails (#2710).

The fork worktree and its branch exist after ``git worktree add`` but before
the snapshot clone and journal seeding run. If one of those raises - or a
cancellation is delivered mid-seed - both the worktree and the branch must be
undone rather than left behind for a later ``git worktree list`` pass.

These tests run against a real git repository so ``git worktree list`` and
``git branch --list`` reflect the true on-disk state rather than a mock.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from bernstein.core.orchestration.run_session import RunSession, sessions_dir_for
from bernstein.core.sessions.fork import fork_session


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialise an empty git repository with one commit."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def parent_session(repo: Path) -> RunSession:
    """Create + persist a parent run session inside *repo*."""
    sdir = sessions_dir_for(repo)
    sdir.mkdir(parents=True, exist_ok=True)
    session = RunSession.create(goal="build a feature", run_seed=42)
    session.tasks = [
        {"id": "t-1", "role": "backend", "title": "implement", "status": "in_progress"},
    ]
    session.save(sdir)
    return session


def _worktrees(repo: Path) -> list[str]:
    return sorted(
        line
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    )


def _branches(repo: Path) -> list[str]:
    return sorted(
        _git(repo, "branch", "--list", "--format=%(refname:short)").splitlines()
    )


def test_fork_raise_after_worktree_exists_leaves_no_worktree_or_branch(
    repo: Path, parent_session: RunSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_wt = _worktrees(repo)
    before_br = _branches(repo)

    sentinel = RuntimeError("snapshot boom")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sentinel

    # _clone_session_snapshot is the first step after the fork worktree +
    # branch exist, so raising here proves the undo fires while they are live.
    monkeypatch.setattr(
        "bernstein.core.sessions.fork._clone_session_snapshot", boom
    )

    with pytest.raises(RuntimeError) as exc_info:
        fork_session(
            parent_session_id=parent_session.session_id,
            fork_label="leak-raise",
            repo_root=repo,
        )

    # The original exception must propagate verbatim.
    assert exc_info.value is sentinel

    assert _worktrees(repo) == before_wt
    assert _branches(repo) == before_br


def test_fork_cancellation_after_worktree_exists_leaves_no_worktree_or_branch(
    repo: Path, parent_session: RunSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_wt = _worktrees(repo)
    before_br = _branches(repo)

    # A cancellation is a BaseException, not an Exception; ``except Exception``
    # would let it escape and leak. This pins ``except BaseException``.
    def cancel(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "bernstein.core.sessions.fork._clone_session_snapshot", cancel
    )

    with pytest.raises(asyncio.CancelledError):
        fork_session(
            parent_session_id=parent_session.session_id,
            fork_label="leak-cancel",
            repo_root=repo,
        )

    assert _worktrees(repo) == before_wt
    assert _branches(repo) == before_br
