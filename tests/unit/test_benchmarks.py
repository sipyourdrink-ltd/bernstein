"""TEST-011: Performance benchmarks for critical paths.

Uses pytest-benchmark to measure timing of:
- TaskStore.create (task creation)
- TaskStore.claim_next (task claiming)
- Lifecycle transition_task (FSM transition)
- Task.from_dict (deserialization)
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.models import Task, TaskStatus
from bernstein.core.task_store import TaskStore

from bernstein.core.lifecycle import transition_task

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeCreateRequest:
    """Minimal object satisfying the TaskCreateRequest protocol."""

    def __init__(self, title: str = "bench-task", role: str = "backend") -> None:
        self.title = title
        self.description = "benchmark task"
        self.role = role
        self.priority = 2
        self.scope = "medium"
        self.complexity = "medium"
        self.estimated_minutes: int | None = None
        self.depends_on: list[str] = []
        self.parent_task_id: str | None = None
        self.depends_on_repo: str | None = None
        self.owned_files: list[str] = []
        self.tenant_id = "default"
        self.cell_id: str | None = None
        self.repo: str | None = None
        self.task_type = "standard"
        self.upgrade_details: dict[str, Any] | None = None
        self.model: str | None = None
        self.effort: str | None = None
        self.batch_eligible = False
        self.approval_required = False
        self.eu_ai_act_risk = "minimal"
        self.risk_level = "low"
        self.completion_signals: list[Any] = []
        self.slack_context: dict[str, Any] | None = None
        self.parent_session_id: str | None = None


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    return TaskStore(jsonl)


def _run(coro: Any) -> Any:
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def test_bench_task_create(benchmark: Any, store: TaskStore) -> None:
    """Benchmark: creating a single task."""

    def _create() -> None:
        _run(store.create(_FakeCreateRequest(title=f"b-{uuid.uuid4().hex[:6]}")))

    benchmark(_create)


#: More claims than the 50 tasks the benchmark used to seed once, so a run past them would show.
_CLAIM_ROUNDS = 60


def _claim_pair(store: TaskStore) -> tuple[Any, Any]:
    """Return an untimed ``setup`` that seeds one claimable task, and a timed ``claim`` of it.

    The benchmark used to seed 50 tasks once and reuse one destructive callback for warmup,
    calibration and measurement, so every call past the 50th measured an empty lookup (#7873).
    """
    claimed: list[Task] = []

    def _setup() -> tuple[tuple[()], dict[str, Any]]:
        _run(store.create(_FakeCreateRequest(title=f"claim-{uuid.uuid4().hex[:6]}", role="backend")))
        return (), {}

    def _claim() -> None:
        task = _run(store.claim_next("backend"))
        assert task is not None, "a measured claim found no claimable task"
        claimed.append(task)

    _claim.claimed = claimed  # type: ignore[attr-defined]
    return _setup, _claim


def test_bench_task_claim_next(benchmark: Any, store: TaskStore) -> None:
    """Benchmark: claiming the next task for a role. Every measured call claims a task."""
    setup, claim = _claim_pair(store)
    benchmark.pedantic(claim, setup=setup, rounds=_CLAIM_ROUNDS, iterations=1, warmup_rounds=0)
    assert claim.claimed, "the benchmark measured no claim"


def test_claim_pair_claims_a_task_on_every_call_past_fifty(store: TaskStore) -> None:
    """The same setup and claim, run more than 50 times without the benchmark, never come back empty."""
    setup, claim = _claim_pair(store)
    for _ in range(_CLAIM_ROUNDS):
        setup()
        claim()
    assert len(claim.claimed) == _CLAIM_ROUNDS
    assert len({task.id for task in claim.claimed}) == _CLAIM_ROUNDS


def test_bench_lifecycle_transition(benchmark: Any) -> None:
    """Benchmark: lifecycle FSM transition_task call."""

    def _transition() -> None:
        task = Task(
            id=str(uuid.uuid4()),
            title="bench",
            description="test",
            role="backend",
            status=TaskStatus.OPEN,
        )
        transition_task(task, TaskStatus.CLAIMED)

    benchmark(_transition)


def test_bench_task_from_dict(benchmark: Any) -> None:
    """Benchmark: Task.from_dict deserialization."""
    raw: dict[str, Any] = {
        "id": "bench-001",
        "title": "Benchmark task",
        "description": "A task for benchmarking",
        "role": "backend",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "status": "open",
        "depends_on": [],
        "owned_files": ["src/foo.py"],
        "assigned_agent": None,
        "result_summary": None,
        "tenant_id": "default",
        "task_type": "standard",
    }

    def _deserialize() -> None:
        Task.from_dict(raw)

    benchmark(_deserialize)


def test_bench_status_summary(benchmark: Any, store: TaskStore) -> None:
    """Benchmark: computing status_summary with many tasks."""
    # Pre-populate with 100 tasks
    for i in range(100):
        role = ["backend", "qa", "frontend"][i % 3]
        _run(store.create(_FakeCreateRequest(title=f"sum-{i}", role=role)))

    def _summary() -> None:
        store.status_summary()

    benchmark(_summary)


def test_bench_list_tasks_filtered(benchmark: Any, store: TaskStore) -> None:
    """Benchmark: filtered list_tasks scan with combined predicates.

    The single-pass filter implementation must stay competitive with the
    old per-predicate chain on a moderately sized store.  This benchmark
    exists so a future regression that re-introduces N passes is caught
    by the perf gate rather than only in production.
    """
    # Pre-populate with 1000 tasks spread across roles, cells and tenants.
    for i in range(1000):
        req = _FakeCreateRequest(title=f"flt-{i}", role=["backend", "qa", "frontend"][i % 3])
        req.cell_id = f"cell-{i % 5}"
        req.tenant_id = "team-a" if i % 2 == 0 else "team-b"
        _run(store.create(req))

    def _filter() -> None:
        store.list_tasks(
            status="open",
            cell_id="cell-1",
            tenant_id="team-a",
        )

    benchmark(_filter)
