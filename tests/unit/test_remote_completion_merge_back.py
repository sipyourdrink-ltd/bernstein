"""A PASSed remote completion lands on the run branch (issue #6611).

A worker outside the host completes over HTTP, so the janitor has no session
to reap and the merge-back ``_reap_and_cleanup_session`` performs for a local
agent never ran. These tests pin the merge-back the verdict pass now performs
for the branch the alive-exit pass materialised.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bernstein.core.models import CompletionSignal, Task, TaskType

from bernstein.core.git.git_pr import MergeResult
from bernstein.core.tasks import task_lifecycle
from bernstein.core.tasks.task_lifecycle import (
    _enqueue_alive_exit_janitor_pass,
    _process_single_completed_task,
)

_KEY = b"k" * 32
_TASK = "T-remote"
_BRANCH = f"agent/remote-{_TASK}-v1"
_SESSION = f"remote-{_TASK}-v1"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo_with_remote_branch(tmp_path: Path) -> Path:
    """A run checkout on a non-default trunk with the ingested branch beside it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    # The merge path commits with the repository identity, and a CI runner has
    # no global one; the ``-c`` flags in ``_git`` cover only its own calls.
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@x")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "checkout", "-q", "-b", _BRANCH)
    (repo / "planned.txt").write_text("remote work\n", encoding="utf-8")
    _git(repo, "add", "planned.txt")
    _git(repo, "commit", "-q", "-m", "remote artifact")
    _git(repo, "checkout", "-q", "trunk")
    return repo


def _task() -> Task:
    task = Task(
        id=_TASK,
        title="t",
        description="d",
        role="qa",
        task_type=TaskType.STANDARD,
        status=task_lifecycle.TaskStatus.DONE,
    )
    task.completion_signals = [CompletionSignal(type="path_exists", value="planned.txt")]
    return task


class _Client:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, url: str, json: Any = None, **_: Any) -> Any:
        self.posts.append(url)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {})


class _Spawner:
    """Records the merge and cleanup the lifecycle asks for."""

    def __init__(self, merge_result: MergeResult | None) -> None:
        self.merge_result = merge_result
        self.merged: list[Any] = []
        self.cleaned: list[str] = []
        self.worktree_present_at_merge: bool | None = None
        self.workdir = Path()
        self.registered: dict[str, Path] = {}

    def register_remote_worktree(self, session_id: str, worktree_path: Path) -> None:
        self.registered[session_id] = worktree_path

    def get_worktree_path(self, session_id: str) -> Path | None:
        return self.registered.get(session_id)

    def reap_completed_agent(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("a remote completion has no session to reap")

    def cleanup_worktree(self, session_id: str) -> None:
        self.cleaned.append(session_id)
        self.registered.pop(session_id, None)

    def merge_remote_branch(self, session: Any) -> MergeResult | None:
        self.merged.append(session)
        self.worktree_present_at_merge = (self.workdir / ".sdd" / "worktrees" / session.id / "planned.txt").exists()
        if self.merge_result is not None and self.merge_result.success:
            # Land it for real, so the branch is merged when cleanup runs.
            _git(self.workdir, "merge", "-q", "--no-ff", "-m", "land", f"agent/{session.id}")
        return self.merge_result


def _orch(repo: Path, spawner: _Spawner) -> Any:
    spawner.workdir = repo
    bulletins: list[tuple[str, str]] = []
    return SimpleNamespace(
        _executor=None,
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _task_to_session={},
        _find_session_for_task=lambda _tid: None,
        _wal_writer=None,
        _spawner=spawner,
        _client=_Client(),
        _post_bulletin=lambda kind, text: bulletins.append((kind, text)),
        _bulletins=bulletins,
        _record_provider_health=lambda *a, **k: None,
        _cost_tracker=SimpleNamespace(spent_usd=0.0, record_cumulative=lambda *a, **k: None, save=lambda *a, **k: None),
        _notify=lambda *a, **k: None,
        _workdir=repo,
        _evolution=None,
        _approval_gate=None,
        _recorder=None,
        _processed_done_tasks={},
    )


def _verdict(passed: bool) -> dict[str, Any]:
    failures: list[str] = [] if passed else ["path_exists: planned.txt"]
    return {_TASK: SimpleNamespace(result=lambda timeout=None: (passed, failures), done=lambda: True)}


def _tick_result() -> Any:
    return SimpleNamespace(
        verified=[],
        verification_failures=[],
        spawned=[],
        reaped=[],
        retried=[],
        errors=[],
        dry_run_planned=[],
        open_tasks=0,
    )


def test_alive_exit_pass_records_the_branch_it_verified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from bernstein.core.git import remote_diff_ingest

    submitted: list[tuple[Any, ...]] = []
    orch = SimpleNamespace(
        _executor=SimpleNamespace(submit=lambda fn, *a, **k: submitted.append((fn, a)) or SimpleNamespace()),
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _task_to_session={},
        _workdir=tmp_path,
        _find_session_for_task=lambda _tid: None,
    )
    monkeypatch.setattr(remote_diff_ingest, "ingest_remote_diff", lambda *a, **k: _BRANCH)
    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", lambda: _KEY)

    _enqueue_alive_exit_janitor_pass(orch, _task(), reason="alive_exit_tick")

    assert orch._remote_completion_branches == {_TASK: _BRANCH}


def test_passed_remote_completion_merges_closes_and_cleans_up(tmp_path: Path) -> None:
    repo = _repo_with_remote_branch(tmp_path)
    spawner = _Spawner(MergeResult(success=True, conflicting_files=[], merge_commit="abc123"))
    orch = _orch(repo, spawner)
    orch._remote_completion_branches = {_TASK: _BRANCH}

    _process_single_completed_task(orch, _task(), _verdict(True), _tick_result())

    (session,) = spawner.merged
    assert (session.id, session.task_ids, session.role) == (_SESSION, [_TASK], "qa")
    # The branch was checked out beside the run checkout for the merge gates.
    assert spawner.worktree_present_at_merge is True
    assert spawner.cleaned == [_SESSION]
    assert orch._client.posts == [f"http://127.0.0.1:8052/tasks/{_TASK}/close"]
    assert orch._remote_completion_branches == {}
    # Nothing of the merge's own scaffolding is left behind.
    assert not (repo / ".sdd" / "worktrees" / _SESSION).exists()
    assert _BRANCH not in _git(repo, "branch", "--list", _BRANCH)


def test_non_conflict_merge_failure_takes_the_reopen_budget_and_keeps_the_branch(tmp_path: Path) -> None:
    repo = _repo_with_remote_branch(tmp_path)
    spawner = _Spawner(MergeResult(success=False, conflicting_files=[], error="unrelated histories"))
    orch = _orch(repo, spawner)
    orch._remote_completion_branches = {_TASK: _BRANCH}

    _process_single_completed_task(orch, _task(), _verdict(True), _tick_result())

    assert len(spawner.merged) == 1
    assert spawner.cleaned == []
    assert orch._client.posts == [f"http://127.0.0.1:8052/tasks/{_TASK}/reopen"]
    assert any(kind == "alert" and "merge-back failed" in text for kind, text in orch._bulletins)
    assert (repo / ".sdd" / "worktrees" / _SESSION / "planned.txt").exists()
    assert _BRANCH in _git(repo, "branch", "--list", _BRANCH)


def test_failed_verdict_does_not_merge(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    repo = _repo_with_remote_branch(tmp_path)
    spawner = _Spawner(MergeResult(success=True, conflicting_files=[]))
    orch = _orch(repo, spawner)
    orch._remote_completion_branches = {_TASK: _BRANCH}

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.task_lifecycle"):
        _process_single_completed_task(orch, _task(), _verdict(False), _tick_result())

    assert spawner.merged == []
    assert orch._client.posts == [f"http://127.0.0.1:8052/tasks/{_TASK}/reopen"]
    assert "remote_merge_back" in caplog.text
    assert "verdict=FAIL" in caplog.text


def test_completion_without_an_ingested_branch_is_unchanged(tmp_path: Path) -> None:
    repo = _repo_with_remote_branch(tmp_path)
    spawner = _Spawner(MergeResult(success=True, conflicting_files=[]))
    orch = _orch(repo, spawner)

    _process_single_completed_task(orch, _task(), _verdict(True), _tick_result())

    assert spawner.merged == []
    assert spawner.cleaned == []
    assert orch._client.posts == []


def test_spawner_merge_remote_branch_lands_the_branch_on_the_run_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawner method runs the real merge path, gates included, on a real repo."""
    from bernstein.core.agents.spawner_core import AgentSpawner
    from bernstein.core.agents.spawner_merge import merge_worktree_branch
    from bernstein.core.tasks.models import AgentSession

    repo = _repo_with_remote_branch(tmp_path)
    monkeypatch.setenv("BERNSTEIN_LOCAL_ONLY", "1")
    fake_self = SimpleNamespace(
        _workdir=repo,
        _merge_locks={},
        _merge_worktree_branch=lambda sid, repo_root=None: merge_worktree_branch(sid, repo, repo_root=repo_root),
        _merge_queue=None,
        _quality_gate_config=None,
    )
    session = AgentSession(id=_SESSION, role="qa", task_ids=[_TASK], status="dead")

    result = AgentSpawner.merge_remote_branch(fake_self, session)

    assert result is not None
    assert result.success, result
    assert (repo / "planned.txt").read_text(encoding="utf-8") == "remote work\n"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "trunk"


def test_failed_checkout_never_reaches_the_merge_and_keeps_the_branch(tmp_path: Path) -> None:
    """A branch that cannot be checked out is not merged with the run checkout standing in for it."""
    repo = _repo_with_remote_branch(tmp_path)
    spawner = _Spawner(MergeResult(success=True, conflicting_files=[]))
    orch = _orch(repo, spawner)
    orch._remote_completion_branches = {_TASK: "agent/remote-T-remote-v9"}  # no such branch

    _process_single_completed_task(orch, _task(), _verdict(True), _tick_result())

    assert spawner.merged == []
    assert spawner.registered == {}
    # Non-conflict failure: the bounded merge-failure budget reopens the task.
    assert orch._client.posts == [f"http://127.0.0.1:8052/tasks/{_TASK}/reopen"]
    assert any(kind == "alert" and "could not check out" in text for kind, text in orch._bulletins)
    assert _BRANCH in _git(repo, "branch", "--list", _BRANCH)


def test_a_negative_verification_gate_blocks_the_merge_and_grades_the_branch_tree(tmp_path: Path) -> None:
    """The local path's verification gates run on the remote branch's tree, and a FAIL stops the merge."""
    repo = _repo_with_remote_branch(tmp_path)
    spawner = _Spawner(MergeResult(success=True, conflicting_files=[]))
    orch = _orch(repo, spawner)
    orch._remote_completion_branches = {_TASK: _BRANCH}
    graded: list[Path] = []

    def _run(task: Any, run_dir: Path, workdir: Path, config: Any) -> Any:
        graded.append(Path(run_dir))
        assert (Path(run_dir) / "planned.txt").exists(), "the gate must see the branch's file"
        failed = SimpleNamespace(gate="lint", blocked=True, passed=False)
        return SimpleNamespace(passed=False, gate_results=[failed])

    orch._quality_gate_config = object()
    orch._gate_coalescer = SimpleNamespace(run=_run)

    _process_single_completed_task(orch, _task(), _verdict(True), _tick_result())

    assert graded == [repo / ".sdd" / "worktrees" / _SESSION]
    assert spawner.merged == []
    assert orch._client.posts == [f"http://127.0.0.1:8052/tasks/{_TASK}/reopen"]
    assert spawner.cleaned == [_SESSION]
    assert not (repo / ".sdd" / "worktrees" / _SESSION).exists()
    # Unmerged work is never force-deleted.
    assert _BRANCH in _git(repo, "branch", "--list", _BRANCH)


def test_approval_pr_mode_opens_the_pr_from_the_remote_worktree(tmp_path: Path) -> None:
    """PR mode needs the worktree registered before the approval gate runs."""
    repo = _repo_with_remote_branch(tmp_path)
    spawner = _Spawner(MergeResult(success=True, conflicting_files=[]))
    orch = _orch(repo, spawner)
    orch._remote_completion_branches = {_TASK: _BRANCH}
    orch._config.pr_labels = []
    created: list[dict[str, Any]] = []

    def _create_pr(task: Any, **kwargs: Any) -> str:
        created.append({"task": task.id, **kwargs})
        assert (Path(kwargs["worktree_path"]) / "planned.txt").exists()
        return "https://example.invalid/pull/1"

    orch._approval_gate = SimpleNamespace(
        evaluate=lambda task, **kwargs: SimpleNamespace(rejected=False, approved=False, resolution=None),
        create_pr=_create_pr,
    )

    _process_single_completed_task(orch, _task(), _verdict(True), _tick_result())

    assert [(c["task"], c["session_id"], Path(c["worktree_path"])) for c in created] == [
        (_TASK, _SESSION, repo / ".sdd" / "worktrees" / _SESSION)
    ]
    assert spawner.merged == []
    # Held for approval: not closed, not reopened, and the work is kept.
    assert orch._client.posts == []
    assert spawner.cleaned == []
    assert (repo / ".sdd" / "worktrees" / _SESSION / "planned.txt").exists()
    assert _BRANCH in _git(repo, "branch", "--list", _BRANCH)


def test_an_approval_hold_without_a_pr_keeps_the_worktree_and_branch(tmp_path: Path) -> None:
    """A rejected or timed-out approval holds the merge; it must not destroy the verified work."""
    repo = _repo_with_remote_branch(tmp_path)
    spawner = _Spawner(MergeResult(success=True, conflicting_files=[]))
    orch = _orch(repo, spawner)
    orch._remote_completion_branches = {_TASK: _BRANCH}
    orch._approval_gate = SimpleNamespace(
        evaluate=lambda task, **kwargs: SimpleNamespace(rejected=True, approved=False, resolution="timed_out"),
    )

    _process_single_completed_task(orch, _task(), _verdict(True), _tick_result())

    assert spawner.merged == []
    assert spawner.cleaned == []
    assert orch._client.posts == []
    assert (repo / ".sdd" / "worktrees" / _SESSION / "planned.txt").exists()
    assert _BRANCH in _git(repo, "branch", "--list", _BRANCH)


def test_a_merge_with_no_result_is_not_treated_as_landed(tmp_path: Path) -> None:
    """``merge_remote_branch`` is typed ``MergeResult | None``; None must not close the task."""
    repo = _repo_with_remote_branch(tmp_path)
    spawner = _Spawner(None)
    orch = _orch(repo, spawner)
    orch._remote_completion_branches = {_TASK: _BRANCH}

    _process_single_completed_task(orch, _task(), _verdict(True), _tick_result())

    assert len(spawner.merged) == 1
    assert orch._client.posts == [f"http://127.0.0.1:8052/tasks/{_TASK}/reopen"]
    assert any(kind == "alert" and "no merge result" in text for kind, text in orch._bulletins)
    assert _BRANCH in _git(repo, "branch", "--list", _BRANCH)
