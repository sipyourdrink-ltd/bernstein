"""Unit tests for TaskStatus.REFUSED and TaskStore.refuse (#2244).

Refusals are terminal task states distinct from failure: a worker that
cannot proceed reports a typed refusal and the task lands in REFUSED,
never DONE and never FAILED. The refusal payload, contract version, and
validation outcome are persisted on the task record so the ledger entry
is self-describing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.security.audit import AuditLog
from bernstein.core.tasks.contracts import (
    WORKER_CONTRACT_VERSION,
    RefusalKind,
    WorkerRefusal,
)
from bernstein.core.tasks.lifecycle import (
    TASK_TRANSITIONS,
    IllegalTransitionError,
    set_audit_log,
    transition_task,
)
from bernstein.core.tasks.models import Task, TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore

# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------


class TestRefusedTransitions:
    @pytest.mark.parametrize(
        "src",
        [TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS],
    )
    def test_legal_source_states(self, src: TaskStatus) -> None:
        assert (src, TaskStatus.REFUSED) in TASK_TRANSITIONS

    @pytest.mark.parametrize(
        "src",
        [
            TaskStatus.DONE,
            TaskStatus.CLOSED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.ABANDONED,
            TaskStatus.REFUSED,
        ],
    )
    def test_refuse_blocked_from_terminal_states(self, src: TaskStatus) -> None:
        assert (src, TaskStatus.REFUSED) not in TASK_TRANSITIONS

    def test_refused_is_terminal(self) -> None:
        # No outgoing edges: refused tasks never silently resume.
        outgoing = [pair for pair in TASK_TRANSITIONS if pair[0] is TaskStatus.REFUSED]
        assert outgoing == []

    def test_transition_task_to_refused_succeeds(self) -> None:
        task = Task(id="T-1", title="t", description="d", role="backend", status=TaskStatus.IN_PROGRESS)
        event = transition_task(task, TaskStatus.REFUSED, actor="test", reason="underspecified")
        assert task.status is TaskStatus.REFUSED
        assert event.to_status == "refused"

    def test_transition_from_refused_is_rejected(self) -> None:
        task = Task(id="T-1", title="t", description="d", role="backend", status=TaskStatus.REFUSED)
        with pytest.raises(IllegalTransitionError):
            transition_task(task, TaskStatus.OPEN)


# ---------------------------------------------------------------------------
# TaskStore.refuse
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> TaskStore:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return TaskStore(runtime / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")


async def _create(store: TaskStore, **overrides: Any) -> Task:
    base: dict[str, Any] = {
        "id": overrides.pop("id", "T-1"),
        "title": "t",
        "description": "d",
        "role": "backend",
        "status": TaskStatus.IN_PROGRESS,
    }
    base.update(overrides)
    task = Task(**base)
    store._tasks[task.id] = task  # type: ignore[attr-defined]
    store._index_add(task)  # type: ignore[attr-defined]
    return task


def _refusal() -> WorkerRefusal:
    return WorkerRefusal(
        kind=RefusalKind.UNDERSPECIFIED,
        detail="The spec does not name the auth backend.",
        question="Which auth backend should this target?",
    )


@pytest.mark.asyncio
class TestTaskStoreRefuse:
    async def test_refuse_marks_status_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _create(store)
        result = await store.refuse("T-1", _refusal())
        assert result.status is TaskStatus.REFUSED

    async def test_refuse_records_contract_metadata(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _create(store)
        result = await store.refuse("T-1", _refusal())
        assert result.metadata["contract_version"] == WORKER_CONTRACT_VERSION
        assert result.metadata["contract_validation"] == "refused:underspecified"
        assert result.metadata["refusal"]["kind"] == "underspecified"
        assert result.metadata["refusal"]["question"].startswith("Which auth backend")

    async def test_refuse_sets_terminal_reason_and_summary(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _create(store)
        result = await store.refuse("T-1", _refusal())
        assert result.terminal_reason == "refused:underspecified"
        assert result.result_summary == "The spec does not name the auth backend."
        assert result.completed_at is not None

    async def test_refuse_unknown_task_raises_keyerror(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(KeyError):
            await store.refuse("missing", _refusal())

    async def test_refuse_from_done_raises_illegal_transition(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _create(store, status=TaskStatus.DONE)
        with pytest.raises(IllegalTransitionError):
            await store.refuse("T-1", _refusal())

    async def test_refused_distinct_from_failed_in_counts(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _create(store, id="T-1")
        await _create(store, id="T-2")
        await store.refuse("T-1", _refusal())
        await store.fail("T-2", "boom")
        counts = store.count_by_status()
        assert counts["refused"] == 1
        assert counts["failed"] == 1

    async def test_status_summary_reports_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _create(store)
        await store.refuse("T-1", _refusal())
        summary = store.status_summary()
        assert summary["refused"] == 1
        assert summary["failed"] == 0

    async def test_refuse_appends_archive_row(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _create(store)
        await store.refuse("T-1", _refusal())
        archive = tmp_path / "archive" / "tasks.jsonl"
        rows = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert any(r["task_id"] == "T-1" and r["status"] == "refused" for r in rows)

    async def test_refuse_writes_contract_audit_event(self, tmp_path: Path) -> None:
        audit = AuditLog(tmp_path / "audit", key=b"test-key-0123456789")
        set_audit_log(audit)
        try:
            store = _store(tmp_path)
            await _create(store)
            await store.refuse("T-1", _refusal())
        finally:
            set_audit_log(None)  # type: ignore[arg-type]
        valid, errors = audit.verify()
        assert valid, errors
        events = [
            json.loads(line)
            for f in sorted((tmp_path / "audit").glob("*.jsonl"))
            for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        contract_events = [e for e in events if e["event_type"] == "task.contract_validation"]
        assert len(contract_events) == 1
        details = contract_events[0]["details"]
        assert details["contract_version"] == WORKER_CONTRACT_VERSION
        assert details["outcome"] == "refused:underspecified"
