"""Regression tests for issue #4541.

``mark_chunk_complete`` and ``reduce_swarm`` had zero callers anywhere in the
tree, so a swarm migration's checkpoint never learned that a chunk finished:
a restart re-spawned purely from task-id memory and the reduce step that
should summarize the swarm could never run.

These tests drive real terminal transitions through
``_apply_janitor_verdict_action`` / ``_apply_merge_failure_action`` (the
lifecycle hook), never ``mark_chunk_complete`` called by hand, and pin the
four scenarios from the issue's acceptance criteria:

* a completed chunk is marked in the swarm checkpoint
* a restart re-spawns only chunks not verified complete (never-run or
  verified-failed), not whatever is sitting in task-id memory
* the last chunk landing runs reduce exactly once and its report is
  recorded durably
* a failed chunk is recorded as failed, distinguishable from one that never
  ran
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.tasks.models import Task, TaskStatus
from bernstein.core.tasks.swarm_migration import (
    MigrationPlan,
    _checkpoint_path,
    spawn_swarm,
)
from bernstein.core.tasks.task_lifecycle import (
    _apply_janitor_verdict_action,
    _apply_merge_failure_action,
)


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    """Records POSTs made by the orchestrator janitor/merge-verdict actions."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, json: dict[str, Any] | None = None, **_kwargs: Any) -> _FakeResponse:
        self.posts.append((url, json or {}))
        return _FakeResponse()


class _RecordingStore:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._counter = 0
        # Ids this store still reports in flight (issue #4624). A fresh store
        # knows none, which models a crash-restart where the server lost the
        # task; the same store re-queried models a mid-swarm re-run.
        self.active_ids: set[str] = set()

    def create_sync(self, body: dict[str, Any]) -> str:
        self._counter += 1
        self.bodies.append(body)
        task_id = f"task-{self._counter:03d}"
        self.active_ids.add(task_id)
        return task_id

    def is_task_active(self, task_id: str) -> bool:
        return task_id in self.active_ids


def _make_repo(tmp_path: Path, files: list[str]) -> Path:
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# placeholder\n", encoding="utf-8")
    return tmp_path


def _make_orch(repo_root: Path, client: _FakeClient) -> SimpleNamespace:
    orch = SimpleNamespace(
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _client=client,
        _processed_done_tasks={},
        _workdir=repo_root,
        bulletins=[],
    )
    orch._post_bulletin = lambda msg_type, content: orch.bulletins.append((msg_type, content))
    return orch


def _task_for(body: dict[str, Any], task_id: str, *, reopen_count: int = 0) -> Task:
    task = Task(
        id=task_id,
        title=body["title"],
        description=body["description"],
        role=body["role"],
        status=TaskStatus.DONE,
        owned_files=list(body["owned_files"]),
        metadata=dict(body["metadata"]),
    )
    if reopen_count:
        task.metadata["janitor_reopen_count"] = reopen_count
    return task


def _spawn_two_chunk_plan(tmp_path: Path) -> tuple[MigrationPlan, Path, _RecordingStore, list[str]]:
    repo = _make_repo(tmp_path, ["src/a.py", "src/b.py"])
    plan = MigrationPlan(id="p4541", glob="src/*.py", transform_prompt="convert", chunk_size=1)
    store = _RecordingStore()
    ids = spawn_swarm(plan, store, repo)
    assert len(ids) == 2
    return plan, repo, store, ids


def _read_checkpoint(repo: Path, plan_id: str) -> dict[str, Any]:
    return json.loads(_checkpoint_path(repo, plan_id).read_text(encoding="utf-8"))


def test_completed_chunk_is_marked_in_the_swarm_checkpoint(tmp_path: Path) -> None:
    plan, repo, store, ids = _spawn_two_chunk_plan(tmp_path)
    task = _task_for(store.bodies[0], ids[0])
    client = _FakeClient()
    orch = _make_orch(repo, client)

    _apply_janitor_verdict_action(orch, task, janitor_passed=True)

    cp = _read_checkpoint(repo, plan.id)
    assert task.metadata["swarm_chunk_hash"] in cp["completed_chunks"]
    assert task.metadata["swarm_chunk_hash"] not in cp["failed_chunks"]
    # Only one of the two chunks resolved; the plan is not fully reduced yet.
    assert not cp.get("reduced")
    assert orch.bulletins == []


def test_restart_respawns_only_unverified_chunks(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, ["src/a.py", "src/b.py", "src/c.py"])
    plan = MigrationPlan(id="p4541-restart", glob="src/*.py", transform_prompt="convert", chunk_size=1)
    store = _RecordingStore()
    first_ids = spawn_swarm(plan, store, repo)
    assert len(first_ids) == 3

    task_a = _task_for(store.bodies[0], first_ids[0])
    task_b = _task_for(store.bodies[1], first_ids[1], reopen_count=2)  # budget already spent
    # Chunk C (store.bodies[2]) is left untouched: never resolved.

    client = _FakeClient()
    orch = _make_orch(repo, client)
    _apply_janitor_verdict_action(orch, task_a, janitor_passed=True)  # verified complete
    _apply_janitor_verdict_action(orch, task_b, janitor_passed=False)  # verified failed

    # A fresh store models a crash-restart: the server lost its in-memory task
    # state, so no earlier task is reported active and every unfinished chunk
    # respawns. (The mid-swarm re-run where the tasks ARE still live is covered
    # by test_inflight_chunk_is_reused_not_respawned in test_swarm_migration.)
    second_store = _RecordingStore()
    second_ids = spawn_swarm(plan, second_store, repo)

    # Chunk A is verified complete: its id is unchanged and it is not respawned.
    assert second_ids[0] == first_ids[0]
    # B (failed) and C (never resolved, its task gone with the crash) are both
    # "unfinished with no live task", so both respawn.
    assert second_ids[1] != first_ids[1]
    assert second_ids[2] != first_ids[2]
    respawned_files = {tuple(b["owned_files"]) for b in second_store.bodies}
    assert tuple(store.bodies[1]["owned_files"]) in respawned_files
    assert tuple(store.bodies[2]["owned_files"]) in respawned_files
    assert tuple(store.bodies[0]["owned_files"]) not in respawned_files

    # The respawned failure's checkpoint record is cleared, not left stale.
    cp = _read_checkpoint(repo, plan.id)
    assert task_b.metadata["swarm_chunk_hash"] not in cp["failed_chunks"]


def test_last_chunk_completion_runs_reduce_exactly_once(tmp_path: Path) -> None:
    plan, repo, store, ids = _spawn_two_chunk_plan(tmp_path)
    task_0 = _task_for(store.bodies[0], ids[0])
    task_1 = _task_for(store.bodies[1], ids[1], reopen_count=2)

    client = _FakeClient()
    orch = _make_orch(repo, client)
    _apply_janitor_verdict_action(orch, task_0, janitor_passed=True)
    assert orch.bulletins == []  # only 1 of 2 chunks resolved so far

    _apply_janitor_verdict_action(orch, task_1, janitor_passed=False)  # the last chunk

    assert len(orch.bulletins) == 1
    msg_type, content = orch.bulletins[0]
    assert msg_type == "status"
    assert "chunks=1/2" in content
    assert "failed=1" in content

    cp = _read_checkpoint(repo, plan.id)
    assert cp["reduced"] is True
    assert cp["report"]["total_chunks"] == 2
    assert cp["report"]["passed_chunks"] == 1
    assert cp["report"]["failed_chunks"] == 1

    # Re-processing an already-terminal chunk must not run reduce a second time.
    _apply_janitor_verdict_action(orch, task_0, janitor_passed=True)
    assert len(orch.bulletins) == 1


def test_failed_chunk_is_recorded_as_failed_not_missing(tmp_path: Path) -> None:
    plan, repo, store, ids = _spawn_two_chunk_plan(tmp_path)
    task_0 = _task_for(store.bodies[0], ids[0], reopen_count=2)
    never_run_hash = store.bodies[1]["metadata"]["swarm_chunk_hash"]

    client = _FakeClient()
    orch = _make_orch(repo, client)
    _apply_janitor_verdict_action(orch, task_0, janitor_passed=False)

    cp = _read_checkpoint(repo, plan.id)
    failed_hash = task_0.metadata["swarm_chunk_hash"]
    assert failed_hash in cp["failed_chunks"]
    assert failed_hash not in cp["completed_chunks"]
    assert cp["failed_chunks"][failed_hash]["files"] == task_0.owned_files
    assert "reopen_budget_exhausted" in cp["failed_chunks"][failed_hash]["reason"]

    # Distinguishable from a chunk that never ran: absent from BOTH sets.
    assert never_run_hash not in cp["completed_chunks"]
    assert never_run_hash not in cp["failed_chunks"]


def test_merge_back_failure_exhausted_budget_marks_chunk_failed(tmp_path: Path) -> None:
    """The second terminal-failure call site (non-conflict merge-back) is wired too."""
    plan, repo, store, ids = _spawn_two_chunk_plan(tmp_path)
    task = _task_for(store.bodies[0], ids[0], reopen_count=2)

    client = _FakeClient()
    orch = _make_orch(repo, client)
    _apply_merge_failure_action(orch, task)

    cp = _read_checkpoint(repo, plan.id)
    assert task.metadata["swarm_chunk_hash"] in cp["failed_chunks"]
    assert "merge_back_failed" in cp["failed_chunks"][task.metadata["swarm_chunk_hash"]]["reason"]


def test_reopen_cycle_does_not_touch_swarm_checkpoint(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A transient FAIL under budget is a retry, not a terminal outcome.

    It must not mark the chunk failed or completed, since it may still
    succeed on the next attempt.
    """
    plan, repo, store, ids = _spawn_two_chunk_plan(tmp_path)
    task = _task_for(store.bodies[0], ids[0])  # reopen_count=0, budget of 2 not spent

    client = _FakeClient()
    orch = _make_orch(repo, client)
    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.task_lifecycle"):
        _apply_janitor_verdict_action(orch, task, janitor_passed=False)

    assert client.posts[0][0].endswith("/reopen")
    cp_path = _checkpoint_path(repo, plan.id)
    # spawn_swarm already created the checkpoint; a reopen must not add this
    # chunk to either terminal set.
    cp = json.loads(cp_path.read_text(encoding="utf-8"))
    assert task.metadata["swarm_chunk_hash"] not in cp["completed_chunks"]
    assert task.metadata["swarm_chunk_hash"] not in cp["failed_chunks"]
    assert orch.bulletins == []
