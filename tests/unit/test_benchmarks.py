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
from bernstein.core.lifecycle import transition_task
from bernstein.core.models import Task, TaskStatus
from bernstein.core.task_store import TaskStore

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


def test_bench_task_claim_next(benchmark: Any, store: TaskStore) -> None:
    """Benchmark: claiming the next task for a role."""

    # Pre-populate with 50 tasks
    for i in range(50):
        _run(store.create(_FakeCreateRequest(title=f"pre-{i}", role="backend")))

    def _claim() -> None:
        _run(store.claim_next("backend"))

    benchmark(_claim)


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
