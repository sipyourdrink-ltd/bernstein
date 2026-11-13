"""Regression tests: ``WorktreeManager.create`` must not leak a worktree or
branch when a post-creation step fails (#2710).

``git worktree add`` creates the worktree *and* the agent branch before the
later setup steps (lock file, environment provisioning, isolation check) run.
If any of those raises - or a cancellation is delivered mid-setup - both the
worktree and the branch must be undone, not left dangling for a later
``git worktree list`` or cleanup pass to treat as live.

These tests run against a real git repository so ``git worktree list`` and
``git branch --list`` reflect the true on-disk state rather than a mock.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import IO

import pytest

from bernstein.core.git import worktree as worktree_module
from bernstein.core.git.git_basic import GitResult
from bernstein.core.git.worktree import WorktreeError, WorktreeManager
from bernstein.core.persistence import file_locks


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
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


def _worktrees(repo: Path) -> list[str]:
    return sorted(
        line for line in _git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")
    )


def _branches(repo: Path) -> list[str]:
    return sorted(_git(repo, "branch", "--list", "--format=%(refname:short)").splitlines())


@pytest.mark.skipif(sys.platform == "win32", reason="Fixture uses an executable POSIX Git hook")
def test_create_checkout_hook_failure_removes_worktree_and_branch(repo: Path) -> None:
    hooks = repo / ".git" / "lane-j-test-hooks"
    hooks.mkdir()
    _git(repo, "config", "core.hooksPath", str(hooks))
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\nprintf '%s\\n' 'test checkout failed' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    manager = WorktreeManager(repo_root=repo)
    before_worktrees = _worktrees(repo)
    before_branches = _branches(repo)

    with pytest.raises(WorktreeError, match="test checkout failed"):
        manager.create("sess-checkout-failure")

    assert _worktrees(repo) == before_worktrees
    assert _branches(repo) == before_branches


def test_create_branch_collision_preserves_existing_ref(repo: Path) -> None:
    session = "existing-branch"
    branch = f"agent/{session}"
    _git(repo, "branch", branch)
    original_ref = _git(repo, "rev-parse", branch)
    manager = WorktreeManager(repo_root=repo)

    with pytest.raises(WorktreeError, match="already exists"):
        manager.create(session)

    assert _git(repo, "rev-parse", branch) == original_ref
    assert not (repo / ".sdd" / "worktrees" / session).exists()


def _signal_lock_contention(monkeypatch: pytest.MonkeyPatch, ready: Event) -> None:
    real_try_lock = file_locks._os_try_lock

    def observe_try_lock(handle: IO[bytes]) -> bool:
        acquired = real_try_lock(handle)
        if not acquired:
            ready.set()
        return acquired

    monkeypatch.setattr(file_locks, "_os_try_lock", observe_try_lock)


def test_failed_checkout_does_not_remove_concurrently_adopted_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator = WorktreeManager(repo, salvage_on_cleanup=False)
    adopter = WorktreeManager(repo, salvage_on_cleanup=False)
    previous = adopter.create("previous-session")
    (previous / "saved.txt").write_text("saved work", encoding="utf-8")
    _git(previous, "add", "saved.txt")
    _git(previous, "commit", "-m", "save work")
    saved_commit = _git(previous, "rev-parse", "HEAD")
    session = "replacement-session"
    checked_out = Event()
    replacement_ready = Event()
    real_add = worktree_module.worktree_add
    _signal_lock_contention(monkeypatch, replacement_ready)

    def fail_after_checkout(cwd: Path, path: Path, branch: str) -> GitResult:
        result = real_add(cwd, path, branch)
        assert result.ok
        checked_out.set()
        # The replacement either finished on an unlocked implementation or
        # tried to acquire the slot lock and is waiting for our rollback.
        assert replacement_ready.wait(timeout=45)
        return GitResult(1, "", "test checkout failed")

    monkeypatch.setattr(worktree_module, "worktree_add", fail_after_checkout)

    def adopt_after_cleanup() -> Path:
        assert checked_out.wait(timeout=45)
        adopter.cleanup(session)
        replacement = adopter.adopt(previous, session)
        replacement_ready.set()
        return replacement

    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(creator.create, session)
        replacing = pool.submit(adopt_after_cleanup)
        with pytest.raises(WorktreeError, match="test checkout failed"):
            creating.result(timeout=100)
        replacement = replacing.result(timeout=100)

    assert (replacement / "saved.txt").read_text(encoding="utf-8") == "saved work"
    assert _git(repo, "rev-parse", f"agent/{session}") == saved_commit


def test_stale_cleanup_rechecks_liveness_after_concurrent_create(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creator = WorktreeManager(repo, salvage_on_cleanup=False)
    janitor = WorktreeManager(repo, salvage_on_cleanup=False)
    before_lock_write = Event()
    janitor_ready = Event()
    real_write = worktree_module.write_worktree_lock
    real_check = janitor._is_session_live
    _signal_lock_contention(monkeypatch, janitor_ready)

    def delay_lock_write(repo_root: Path, session_id: str, pid: int) -> Path:
        before_lock_write.set()
        assert janitor_ready.wait(timeout=45)
        return real_write(repo_root, session_id, pid)

    def observe_liveness(session_id: str) -> bool:
        live = real_check(session_id)
        janitor_ready.set()
        return live

    def scan() -> int:
        assert before_lock_write.wait(timeout=45)
        return janitor.cleanup_all_stale()

    monkeypatch.setattr(worktree_module, "write_worktree_lock", delay_lock_write)
    monkeypatch.setattr(janitor, "_is_session_live", observe_liveness)
    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(creator.create, "new-session")
        cleaning = pool.submit(scan)
        created = creating.result(timeout=100)
        cleaned = cleaning.result(timeout=100)

    assert cleaned == 0
    assert created.is_dir()
    assert "agent/new-session" in _branches(repo)


def test_create_raise_after_worktree_exists_leaves_no_worktree_or_branch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = WorktreeManager(repo_root=repo)
    before_wt = _worktrees(repo)
    before_br = _branches(repo)

    sentinel = RuntimeError("post-creation boom")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sentinel

    # write_worktree_lock is the first step after the worktree + branch exist,
    # so raising here proves the undo fires while the resources are live.
    monkeypatch.setattr("bernstein.core.git.worktree.write_worktree_lock", boom)

    with pytest.raises(RuntimeError) as exc_info:
        mgr.create("sess-raise")

    # The original exception must propagate verbatim - cleanup must not
    # replace the error that caused it.
    assert exc_info.value is sentinel

    assert _worktrees(repo) == before_wt
    assert _branches(repo) == before_br


def test_create_cancellation_after_worktree_exists_leaves_no_worktree_or_branch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = WorktreeManager(repo_root=repo)
    before_wt = _worktrees(repo)
    before_br = _branches(repo)

    # A cancellation is a BaseException, not an Exception; ``except Exception``
    # would let it escape and leak. This pins ``except BaseException``.
    def cancel(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("bernstein.core.git.worktree.write_worktree_lock", cancel)

    with pytest.raises(asyncio.CancelledError):
        mgr.create("sess-cancel")

    assert _worktrees(repo) == before_wt
    assert _branches(repo) == before_br
