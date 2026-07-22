"""Regression tests for issue #2792.

A worker that commits its work in a worktree and then hits a *non-conflict*
merge-back failure (an untracked operator-tree file, the forbidden-path guard,
unrelated histories, a missing branch) must not have its worktree and
``agent/<id>`` branch force-deleted, and its task must not be silently returned
to the open queue for uncapped retry.

These tests pin three behaviours:

* ``_reap_and_cleanup_session`` skips ``cleanup_worktree`` when a merge-back
  failed (preserving the only committed copy on ``agent/<id>``) and still runs
  it on merge success.
* ``_apply_merge_failure_action`` routes the failure through the bounded
  reopen/permanent-fail budget rather than leaving the task open forever.
* ``WorktreeManager.cleanup`` preserves committed-but-unmerged work to the
  graveyard before ``git branch -D`` when cleanup does eventually run.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.git.git_pr import MergeResult
from bernstein.core.git.worktree import WorktreeManager
from bernstein.core.tasks.models import AgentSession, Task, TaskStatus
from bernstein.core.tasks.task_lifecycle import (
    _apply_merge_failure_action,
    _reap_and_cleanup_session,
)


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, json: dict[str, Any] | None = None, **_kwargs: Any) -> _FakeResponse:
        self.posts.append((url, json or {}))
        return _FakeResponse()


class _FakeSpawner:
    """Records whether cleanup ran and returns a canned merge result."""

    def __init__(self, merge_result: MergeResult | None) -> None:
        self._merge_result = merge_result
        self.cleanup_called = False

    def reap_completed_agent(
        self,
        session: AgentSession,
        *,
        skip_merge: bool = False,
        defer_cleanup: bool = False,
    ) -> MergeResult | None:
        return self._merge_result

    def get_worktree_path(self, _session_id: str) -> Path | None:
        return None

    def cleanup_worktree(self, _session_id: str) -> None:
        self.cleanup_called = True


def _make_task() -> Task:
    return Task(
        id="task-2792",
        title="Create hello.txt",
        description="Write hello world",
        role="backend",
        status=TaskStatus.DONE,
    )


def _make_session() -> AgentSession:
    return AgentSession(
        id="writer-8f6311cf",
        role="backend",
        pid=4242,
        task_ids=["task-2792"],
        status="working",
        exit_code=0,
    )


def _make_orch(spawner: _FakeSpawner, tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        _spawner=spawner,
        _workdir=tmp_path,
        _config=SimpleNamespace(ab_test=False),
        _post_bulletin=lambda *_args, **_kwargs: None,
    )


def test_reap_preserves_worktree_on_nonconflict_merge_failure(tmp_path: Path) -> None:
    """A non-conflict merge failure must NOT force-delete the worktree/branch."""
    merge_result = MergeResult(
        success=False,
        conflicting_files=[],
        error="untracked working tree file hello.txt would be overwritten",
    )
    spawner = _FakeSpawner(merge_result)
    orch = _make_orch(spawner, tmp_path)

    _cache_verified, _cache_diff, merge_failed = _reap_and_cleanup_session(
        orch,
        _make_task(),
        _make_session(),
        None,
        True,  # janitor_passed - the worker's own work passed
        False,  # skip_merge
        None,
        0,
    )

    assert merge_failed is True
    assert spawner.cleanup_called is False, "cleanup_worktree must be gated on merge success so agent/<id> survives"


def test_reap_cleans_worktree_on_merge_success(tmp_path: Path) -> None:
    """On a clean merge, cleanup still runs (no behaviour change)."""
    merge_result = MergeResult(success=True, conflicting_files=[], merge_diff="ok")
    spawner = _FakeSpawner(merge_result)
    orch = _make_orch(spawner, tmp_path)

    _cache_verified, _cache_diff, merge_failed = _reap_and_cleanup_session(
        orch,
        _make_task(),
        _make_session(),
        None,
        True,
        False,
        None,
        1,
    )

    assert merge_failed is False
    assert spawner.cleanup_called is True


def test_reap_cleans_worktree_when_merge_skipped(tmp_path: Path) -> None:
    """skip_merge (approval-gate PR path) still cleans up - not a merge failure."""
    spawner = _FakeSpawner(None)
    orch = _make_orch(spawner, tmp_path)

    _cache_verified, _cache_diff, merge_failed = _reap_and_cleanup_session(
        orch,
        _make_task(),
        _make_session(),
        None,
        True,
        True,  # skip_merge
        None,
        0,
    )

    assert merge_failed is False
    assert spawner.cleanup_called is True


def _make_verdict_orch(client: _FakeClient) -> SimpleNamespace:
    return SimpleNamespace(
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _client=client,
        _processed_done_tasks={"task-2792": None},
    )


def test_merge_failure_reopens_under_bounded_budget(caplog: pytest.LogCaptureFixture) -> None:
    """First merge-back failure reopens the same task (bounded), not an open-queue drop."""
    client = _FakeClient()
    orch = _make_verdict_orch(client)
    task = _make_task()

    with caplog.at_level(logging.WARNING, logger="bernstein.core.tasks.task_lifecycle"):
        _apply_merge_failure_action(orch, task)

    assert len(client.posts) == 1
    url, body = client.posts[0]
    assert url.endswith("/tasks/task-2792/reopen")
    assert "merge" in body["reason"].lower()
    assert "task-2792" not in orch._processed_done_tasks
    assert "merge_failure_action: task=task-2792 action=reopen cycle=1/2" in caplog.text


def test_merge_failure_permanent_fails_when_budget_exhausted(caplog: pytest.LogCaptureFixture) -> None:
    """Once the reopen budget is spent the task is permanently failed, never re-queued."""
    client = _FakeClient()
    orch = _make_verdict_orch(client)
    task = _make_task()
    task.metadata["janitor_reopen_count"] = 2  # default budget already spent

    with caplog.at_level(logging.ERROR, logger="bernstein.core.tasks.task_lifecycle"):
        _apply_merge_failure_action(orch, task)

    assert len(client.posts) == 1
    url, body = client.posts[0]
    assert url.endswith("/tasks/task-2792/fail")
    assert "merge" in body["reason"].lower()
    assert "merge_failure_action: task=task-2792 action=permanent_fail" in caplog.text


# --- committed-but-unmerged graveyard preservation (real git) ---------------


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8")


@pytest.fixture
def repo_with_committed_worktree(tmp_path: Path) -> tuple[Path, str]:
    """Real repo whose ``agent/<id>`` worktree holds a committed, unmerged change."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _run(["git", "init", "-b", "main"], repo_root)
    _run(["git", "config", "user.email", "test@example.com"], repo_root)
    _run(["git", "config", "user.name", "Test User"], repo_root)
    _run(["git", "config", "commit.gpgsign", "false"], repo_root)
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "README.md"], repo_root)
    _run(["git", "commit", "-m", "seed"], repo_root)

    session_id = "committed-session"
    worktree_path = repo_root / ".sdd" / "worktrees" / session_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "-b", f"agent/{session_id}", str(worktree_path)], repo_root)
    # Commit the work inside the worktree - clean tree, but a commit that is
    # reachable from no other branch.
    (worktree_path / "hello.txt").write_text("hello from agent\n", encoding="utf-8")
    _run(["git", "add", "hello.txt"], worktree_path)
    _run(["git", "commit", "-m", "add hello.txt"], worktree_path)
    return repo_root, session_id


def test_cleanup_preserves_committed_unmerged_work_to_graveyard(
    repo_with_committed_worktree: tuple[Path, str],
) -> None:
    """cleanup() must graveyard committed-but-unmerged work before branch -D."""
    repo_root, session_id = repo_with_committed_worktree
    tip_sha = subprocess.run(
        ["git", "rev-parse", f"agent/{session_id}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()

    mgr = WorktreeManager(repo_root=repo_root, salvage_on_cleanup=True, salvage_push=False)
    mgr.cleanup(session_id)

    # The agent branch is gone (force-deleted), but the commit survives in the
    # graveyard ref + bundle.
    refs = (
        subprocess.run(
            ["git", "for-each-ref", "--format=%(refname) %(objectname)", "refs/graveyard/"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        .stdout.strip()
        .splitlines()
    )
    assert len(refs) == 1, f"expected one graveyard ref, got {refs!r}"
    ref_name, ref_sha = refs[0].split()
    assert ref_name.startswith(f"refs/graveyard/{session_id}-")
    assert ref_sha == tip_sha, "graveyard ref must point at the committed work"

    bundles = list((repo_root / ".sdd" / "graveyard").glob(f"{session_id}-*.bundle"))
    assert len(bundles) == 1
    assert bundles[0].stat().st_size > 0


def test_cleanup_merged_branch_creates_no_graveyard(
    repo_with_committed_worktree: tuple[Path, str],
) -> None:
    """When the branch is already merged, cleanup must NOT create graveyard refs."""
    repo_root, session_id = repo_with_committed_worktree
    # Merge the agent branch into main so its commit is reachable elsewhere.
    _run(["git", "merge", "--no-ff", "-m", "merge", f"agent/{session_id}"], repo_root)

    mgr = WorktreeManager(repo_root=repo_root, salvage_on_cleanup=True, salvage_push=False)
    mgr.cleanup(session_id)

    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/graveyard/"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    assert refs == "", f"merged branch must not be graveyarded, got {refs!r}"
