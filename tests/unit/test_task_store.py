"""Focused tests for TaskStore CRUD, replay, and indexing behavior."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bernstein.core.models import TaskStatus
from bernstein.core.task_store import TaskStore
from fastapi import HTTPException

from bernstein.core.tasks.models import ApprovalSpec


def _task_request(
    *,
    title: str = "Implement parser",
    description: str = "Write the parser module.",
    role: str = "backend",
    priority: int = 1,
    scope: str = "medium",
    complexity: str = "medium",
    depends_on: list[str] | None = None,
    parent_context: str | None = None,
) -> Any:
    """Build a create-task request object with the TaskCreate attributes TaskStore expects."""
    return SimpleNamespace(
        title=title,
        description=description,
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        estimated_minutes=30,
        depends_on=depends_on or [],
        owned_files=[],
        cell_id=None,
        task_type="standard",
        upgrade_details=None,
        model=None,
        effort=None,
        batch_eligible=False,
        completion_signals=[],
        slack_context=None,
        parent_context=parent_context,
    )


@pytest.mark.anyio
async def test_create_claim_complete_and_archive_task(tmp_path: Path) -> None:
    """A task can be created, claimed, completed, and archived with persisted state."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")

    task = await store.create(_task_request())
    claimed = await store.claim_next("backend")
    completed = await store.complete(task.id, "Parser shipped")
    await store.flush_buffer()

    assert task.status == TaskStatus.DONE
    assert claimed is not None
    assert claimed.id == task.id
    assert completed.result_summary == "Parser shipped"
    assert store.read_archive(limit=1)[0]["task_id"] == task.id


@pytest.mark.anyio
async def test_claim_next_prefers_highest_priority_open_task(tmp_path: Path) -> None:
    """claim_next returns the lowest-numbered priority task first."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    low = await store.create(_task_request(title="low", priority=3))
    high = await store.create(_task_request(title="high", priority=1))

    claimed = await store.claim_next("backend")

    assert claimed is not None
    assert claimed.id == high.id
    assert store.get_task(low.id) is not None


@pytest.mark.anyio
async def test_fail_marks_task_failed_and_writes_archive(tmp_path: Path) -> None:
    """Failing a task records the failure reason and writes an archive record."""
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl", archive_path=archive_path)

    task = await store.create(_task_request())
    await store.claim_by_id(task.id, expected_version=task.version)
    failed = await store.fail(task.id, "unit tests failed")
    await store.flush_buffer()

    assert failed.status == TaskStatus.FAILED
    assert failed.result_summary == "unit tests failed"
    assert store.read_archive(limit=1)[0]["status"] == "failed"


@pytest.mark.anyio
async def test_replay_jsonl_reconstructs_latest_task_state(tmp_path: Path) -> None:
    """Replaying the JSONL log rebuilds the in-memory state from disk."""
    jsonl_path = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl_path)

    task = await store.create(_task_request())
    await store.claim_by_id(task.id, expected_version=task.version)
    await store.flush_buffer()

    replayed = TaskStore(jsonl_path)
    replayed.replay_jsonl()
    restored = replayed.get_task(task.id)

    assert restored is not None
    assert restored.status == TaskStatus.CLAIMED
    assert restored.version == task.version


@pytest.mark.anyio
async def test_replay_jsonl_preserves_subtask_parent_context(tmp_path: Path) -> None:
    """A subtask's parent_context survives the JSONL write/replay round-trip.

    The parent agent's context summary is the only thing tying a subtask to
    the exploration that produced it. If the serialiser drops it, the subtask
    is silently rebuilt without that context after a restart and the spawned
    agent starts cold.
    """
    jsonl_path = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl_path)
    parent_context = "Parent explored src/parser.py and chose a recursive-descent design."

    task = await store.create(_task_request(parent_context=parent_context))
    await store.flush_buffer()

    in_memory = store.get_task(task.id)
    assert in_memory is not None
    assert in_memory.parent_context == parent_context

    replayed = TaskStore(jsonl_path)
    replayed.replay_jsonl()
    restored = replayed.get_task(task.id)

    assert restored is not None
    assert restored.parent_context == parent_context


@pytest.mark.anyio
async def test_claim_by_id_rejects_stale_expected_version(tmp_path: Path) -> None:
    """claim_by_id enforces optimistic locking on task version."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    task = await store.create(_task_request())
    stale_version = task.version
    await store.claim_by_id(task.id, expected_version=stale_version)

    with pytest.raises(ValueError, match="Version conflict"):
        await store.claim_by_id(task.id, expected_version=stale_version)


@pytest.mark.anyio
async def test_create_rejects_missing_dependency_ids(tmp_path: Path) -> None:
    """Creating a task with a nonexistent dependency raises HTTP 422."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    with pytest.raises(HTTPException, match="non-existent"):
        await store.create(_task_request(depends_on=["missing-task"]))


@pytest.mark.anyio
async def test_list_tasks_filters_open_tasks_by_completed_dependencies(tmp_path: Path) -> None:
    """Open-task listing hides tasks whose dependencies are not yet done."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    dependency = await store.create(_task_request(title="dependency"))
    blocked = await store.create(_task_request(title="blocked", depends_on=[dependency.id]))

    open_before = {task.id for task in store.list_tasks(status="open")}
    await store.claim_by_id(dependency.id, expected_version=dependency.version)
    await store.complete(dependency.id, "done")
    open_after = {task.id for task in store.list_tasks(status="open")}

    assert blocked.id not in open_before
    assert blocked.id in open_after


@pytest.mark.anyio
async def test_closed_dependency_still_satisfies_dependents(tmp_path: Path) -> None:
    """A dependency that moved done -> closed keeps its dependents claimable.

    Done tasks transition to closed once their agent is reaped and its branch
    merged (soft-archive via status). Dependents must stay visible in the
    open listing and remain claimable after that transition.
    """
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    dependency = await store.create(_task_request(title="dependency"))
    blocked = await store.create(_task_request(title="blocked", depends_on=[dependency.id]))

    await store.claim_by_id(dependency.id, expected_version=dependency.version)
    await store.complete(dependency.id, "done")
    await store.close(dependency.id)

    open_ids = {task.id for task in store.list_tasks(status="open")}
    assert blocked.id in open_ids

    claimed = await store.claim_by_id(blocked.id, expected_version=blocked.version)
    assert claimed.status == TaskStatus.CLAIMED


@pytest.mark.anyio
async def test_status_summary_reports_counts_by_status(tmp_path: Path) -> None:
    """status_summary aggregates task counts and per-role breakdowns."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    open_task = await store.create(_task_request(title="open", role="backend"))
    claimed_task = await store.create(_task_request(title="claimed", role="backend"))
    failed_task = await store.create(_task_request(title="failed", role="qa"))

    await store.claim_by_id(claimed_task.id, expected_version=claimed_task.version)
    await store.claim_by_id(failed_task.id, expected_version=failed_task.version)
    await store.fail(failed_task.id, "boom")
    await store.flush_buffer()

    summary: dict[str, Any] = store.status_summary()

    assert summary["total"] == 3
    assert summary["open"] == 1
    assert summary["claimed"] == 1
    assert summary["failed"] == 1
    assert store.get_task(open_task.id) is not None


@pytest.mark.anyio
async def test_replay_progress_restores_entries_and_snapshots_after_restart(tmp_path: Path) -> None:
    """audit-023: progress entries + snapshots survive a simulated server restart.

    Writes 10 progress entries and 3 snapshots through one TaskStore, then
    instantiates a fresh store pointing at the same paths and verifies the
    full history is replayed back into memory.
    """
    jsonl_path = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl_path)
    task = await store.create(_task_request(title="progress-persistence"))

    for i in range(10):
        await store.add_progress(task.id, f"step {i}", percent=i * 10)
    for i in range(3):
        store.add_snapshot(
            task.id,
            files_changed=i,
            tests_passing=i * 2,
            errors=0,
            last_file=f"file_{i}.py",
        )
    await store.flush_buffer()

    # Simulate a server restart: brand-new TaskStore, replay from disk only.
    restored = TaskStore(jsonl_path)
    restored.replay_jsonl()
    restored_task = restored.get_task(task.id)

    assert restored_task is not None
    entries = list(restored_task.progress_log)
    assert len(entries) == 10
    assert [entry["message"] for entry in entries] == [f"step {i}" for i in range(10)]
    assert [entry["percent"] for entry in entries] == [i * 10 for i in range(10)]

    snapshots = restored.get_snapshots(task.id)
    assert len(snapshots) == 3
    assert [snap.files_changed for snap in snapshots] == [0, 1, 2]
    assert [snap.last_file for snap in snapshots] == ["file_0.py", "file_1.py", "file_2.py"]

    # The progress file itself must exist under .sdd/runtime/progress/.
    progress_file = jsonl_path.parent / "progress" / f"{task.id}.jsonl"
    assert progress_file.exists()
    lines = progress_file.read_text().splitlines()
    assert len(lines) == 13  # 10 entries + 3 snapshots


def _write_archive(archive_path: Path, records: list[dict[str, Any]]) -> None:
    """Write archive rows verbatim, as a hand edit or an older writer would."""
    import json

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_read_archive_does_not_coerce_a_stored_tenant_id(tmp_path: Path) -> None:
    """A non-string tenant id is a row that cannot be read, not one to convert.

    The archive is JSON, so the field holds whatever was written. Coercing
    before validating turns `true` into `"True"` and `null` into `"None"`,
    both of which satisfy the identifier rules, and the row is then handed to
    whichever tenant happens to carry that name.
    """
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    _write_archive(
        archive_path,
        [
            {"task_id": "boolean-row", "tenant_id": True},
            {"task_id": "numeric-row", "tenant_id": 123},
            {"task_id": "owned-row", "tenant_id": "True"},
        ],
    )
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl", archive_path=archive_path)

    records = store.read_archive(limit=50, tenant_id="True")

    assert [record["task_id"] for record in records] == ["owned-row"]


def test_read_archive_keeps_filtering_out_malformed_tenant_ids(tmp_path: Path) -> None:
    """A string that no longer normalizes still matches no tenant."""
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    _write_archive(
        archive_path,
        [
            {"task_id": "legacy-row", "tenant_id": "../escape"},
            {"task_id": "owned-row", "tenant_id": "team-a"},
        ],
    )
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl", archive_path=archive_path)

    records = store.read_archive(limit=50, tenant_id="team-a")

    assert [record["task_id"] for record in records] == ["owned-row"]


@pytest.mark.anyio
async def test_count_tasks_matches_len_list_tasks(tmp_path: Path) -> None:
    """count_tasks returns the exact same number len(list_tasks) returns across filters."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")

    t1 = await store.create(_task_request(title="task 1"))
    t2 = await store.create(_task_request(title="task 2"))
    await store.claim_by_id(t1.id, expected_version=t1.version)
    await store.complete(t1.id, "done")

    assert store.count_tasks() == len(store.list_tasks())
    assert store.count_tasks(status="open") == len(store.list_tasks(status="open"))
    assert store.count_tasks(status="done") == len(store.list_tasks(status="done"))
    assert store.count_tasks(status="nonexistent") == len(store.list_tasks(status="nonexistent"))
    assert store.count_tasks(tenant_id=t2.tenant_id) == len(store.list_tasks(tenant_id=t2.tenant_id))


def _approval_spec() -> ApprovalSpec:
    """Return the valid gate spec used by the journal replay tests."""
    return ApprovalSpec(prompt="Approve the deploy", timeout_seconds=42, default_action="approve")


def _record_line(store: TaskStore, task: Any, **overrides: Any) -> str:
    """Serialise a task record with field overrides as one JSONL line."""
    record = dict(store._task_to_record(task))
    record.update(overrides)
    return json.dumps(record) + "\n"


async def _task_with_spec(store: TaskStore, jsonl_path: Path, spec: ApprovalSpec | dict[str, Any] | None) -> Any:
    """Create a task and write a creation line carrying the given approval spec."""
    task = await store.create(_task_request())
    if isinstance(spec, ApprovalSpec):
        task.approval_spec = spec
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text(
        _record_line(
            store,
            task,
            approval_spec=(spec.to_dict() if isinstance(spec, ApprovalSpec) else spec),
        ),
        encoding="utf-8",
    )
    return task


@pytest.mark.anyio
async def test_replay_round_trips_approval_spec(tmp_path: Path) -> None:
    """A valid approval_spec survives the JSONL write/replay round-trip."""
    jsonl_path = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl_path)
    spec = _approval_spec()
    task = await _task_with_spec(store, jsonl_path, spec)

    assert store._task_to_record(task).get("approval_spec") == spec.to_dict()

    replayed = TaskStore(jsonl_path)
    replayed.replay_jsonl()
    restored = replayed.get_task(task.id)

    assert restored is not None
    assert restored.approval_spec == spec


@pytest.mark.anyio
async def test_replay_explicit_none_clears_approval_spec(tmp_path: Path) -> None:
    """An explicit null approval_spec in a later record clears the gate."""
    jsonl_path = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl_path)
    task = await _task_with_spec(store, jsonl_path, _approval_spec())
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(_record_line(store, task, approval_spec=None))

    replayed = TaskStore(jsonl_path)
    replayed.replay_jsonl()
    restored = replayed.get_task(task.id)

    assert restored is not None
    assert restored.approval_spec is None


@pytest.mark.parametrize(
    ("malformed", "reason"),
    [
        ("a string", "str"),
        ([], "list"),
        (False, "bool"),
        (0, "int"),
        ({}, "prompt"),
        ({"prompt": "", "timeout_seconds": 10, "default_action": "reject"}, "non-empty"),
        ({"prompt": "Next gate", "timeout_seconds": None, "default_action": "reject"}, "NoneType"),
    ],
)
@pytest.mark.anyio
async def test_replay_malformed_approval_spec_keeps_valid_policy(
    tmp_path: Path, malformed: object, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed approval_spec values keep the last valid gate and do not abort replay."""
    jsonl_path = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl_path)
    original = _approval_spec()
    task = await _task_with_spec(store, jsonl_path, original)
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(_record_line(store, task, approval_spec=malformed))

    replayed = TaskStore(jsonl_path)
    with caplog.at_level("WARNING"):
        replayed.replay_jsonl()
    restored = replayed.get_task(task.id)

    assert restored is not None
    assert restored.approval_spec == original
    warning = caplog.text
    assert task.id in warning
    assert str(jsonl_path) in warning
    assert "2" in warning
    assert reason in warning

    # A valid record after the corrupt line must still be replayed.
    replacement = ApprovalSpec(prompt="Second gate", timeout_seconds=7, default_action="reject")
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(_record_line(store, task, approval_spec=replacement.to_dict()))
    replayed_again = TaskStore(jsonl_path)
    replayed_again.replay_jsonl()
    latest = replayed_again.get_task(task.id)

    assert latest is not None
    assert latest.approval_spec == replacement


@pytest.mark.anyio
async def test_replay_skips_creation_line_with_malformed_approval_spec(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed: a creation line with a malformed gate is skipped, never dropped to no gate."""
    jsonl_path = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl_path)
    corrupt = await store.create(_task_request(title="gated"))
    other = await store.create(_task_request(title="ungated"))
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text(
        _record_line(
            store,
            corrupt,
            approval_spec={"prompt": "", "timeout_seconds": 10, "default_action": "reject"},
        )
        + _record_line(store, other, approval_spec=None),
        encoding="utf-8",
    )

    replayed = TaskStore(jsonl_path)
    with caplog.at_level("WARNING"):
        replayed.replay_jsonl()

    assert replayed.get_task(corrupt.id) is None
    assert replayed.get_task(other.id) is not None
    warning = caplog.text
    assert corrupt.id in warning
    assert str(jsonl_path) in warning
    assert "1" in warning
    assert "non-empty" in warning
