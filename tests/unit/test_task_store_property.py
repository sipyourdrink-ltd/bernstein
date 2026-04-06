"""TEST-009: Property-based testing for task store using Hypothesis.

Invariants tested:
- No duplicate task IDs after any sequence of creates.
- Status transitions only follow the allowed FSM.
- Status counts are always consistent with the task dict.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.core.lifecycle import TASK_TRANSITIONS, IllegalTransitionError, transition_task
from bernstein.core.models import Task, TaskStatus, TaskType
from bernstein.core.task_store import TaskStore


def _run_async(coro: Any) -> Any:
    """Run an async coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_ROLES = st.sampled_from(["backend", "qa", "frontend", "devops", "security"])
_PRIORITIES = st.integers(min_value=1, max_value=3)
_SCOPES = st.sampled_from(["small", "medium", "large"])
_COMPLEXITIES = st.sampled_from(["low", "medium", "high"])


@st.composite
def task_create_kwargs(draw: st.DrawFn) -> dict[str, Any]:
    """Generate keyword args suitable for TaskStore.create()."""
    return {
        "title": draw(st.text(min_size=1, max_size=60, alphabet=st.characters(categories=("L", "N", "Z")))),
        "description": draw(st.text(min_size=1, max_size=120, alphabet=st.characters(categories=("L", "N", "Z")))),
        "role": draw(_ROLES),
        "priority": draw(_PRIORITIES),
        "scope": draw(_SCOPES),
        "complexity": draw(_COMPLEXITIES),
        "tenant_id": "default",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> TaskStore:
    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    return TaskStore(jsonl)


class _FakeCreateRequest:
    """Minimal object satisfying the TaskCreateRequest protocol."""

    def __init__(self, **kwargs: Any) -> None:
        self.title: str = kwargs.get("title", "t")
        self.description: str = kwargs.get("description", "d")
        self.role: str = kwargs.get("role", "backend")
        self.priority: int = kwargs.get("priority", 2)
        self.scope: str = kwargs.get("scope", "medium")
        self.complexity: str = kwargs.get("complexity", "medium")
        self.estimated_minutes: int | None = None
        self.depends_on: list[str] = []
        self.parent_task_id: str | None = None
        self.depends_on_repo: str | None = None
        self.owned_files: list[str] = []
        self.tenant_id: str = kwargs.get("tenant_id", "default")
        self.cell_id: str | None = None
        self.repo: str | None = None
        self.task_type: str = "standard"
        self.upgrade_details: dict[str, Any] | None = None
        self.model: str | None = None
        self.effort: str | None = None
        self.batch_eligible: bool = False
        self.approval_required: bool = False
        self.eu_ai_act_risk: str = "minimal"
        self.risk_level: str = "low"
        self.completion_signals: list[Any] = []
        self.slack_context: dict[str, Any] | None = None
        self.parent_session_id: str | None = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoDuplicateIDs:
    """Creating N tasks must produce N distinct IDs."""

    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(payloads=st.lists(task_create_kwargs(), min_size=1, max_size=10))
    def test_ids_unique(self, payloads: list[dict[str, Any]], tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        async def _run() -> list[str]:
            ids: list[str] = []
            for kw in payloads:
                task = await store.create(_FakeCreateRequest(**kw))
                ids.append(task.id)
            return ids

        ids = _run_async(_run())
        assert len(ids) == len(set(ids)), "Duplicate task IDs detected"


class TestTransitionsOnlyValid:
    """Only transitions in the FSM table are accepted."""

    _ALL_STATUSES = list(TaskStatus)

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        from_idx=st.integers(min_value=0, max_value=len(list(TaskStatus)) - 1),
        to_idx=st.integers(min_value=0, max_value=len(list(TaskStatus)) - 1),
    )
    def test_transition_matches_table(self, from_idx: int, to_idx: int) -> None:
        from_status = self._ALL_STATUSES[from_idx]
        to_status = self._ALL_STATUSES[to_idx]

        task = Task(
            id=str(uuid.uuid4()),
            title="prop",
            description="test",
            role="backend",
            status=from_status,
        )

        allowed = (from_status, to_status) in TASK_TRANSITIONS

        if allowed:
            transition_task(task, to_status)
            assert task.status == to_status
        else:
            with pytest.raises(IllegalTransitionError):
                transition_task(task, to_status)


class TestCountsConsistent:
    """After a batch of creates the store counts must match the task dict."""

    @settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(n=st.integers(min_value=0, max_value=8))
    def test_counts_match(self, n: int, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        async def _run() -> None:
            for _ in range(n):
                await store.create(_FakeCreateRequest(title=f"task-{uuid.uuid4().hex[:6]}"))

            summary = store.status_summary()
            # All freshly created tasks are OPEN
            assert summary["total"] == n
            assert summary["open"] == n

            # Verify the by_status index is consistent
            open_count = len(store._by_status.get(TaskStatus.OPEN, {}))
            assert open_count == n

        _run_async(_run())


class TestStatusTransitionSequences:
    """Property: applying a valid transition sequence maintains internal consistency."""

    _VALID_SEQUENCES: list[list[TaskStatus]] = [
        [TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS, TaskStatus.DONE, TaskStatus.CLOSED],
        [TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.FAILED, TaskStatus.OPEN],
        [TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS, TaskStatus.FAILED],
        [TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.OPEN],
    ]

    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(seq_idx=st.integers(min_value=0, max_value=3))
    def test_valid_sequence_succeeds(self, seq_idx: int) -> None:
        seq = self._VALID_SEQUENCES[seq_idx]
        task = Task(
            id=str(uuid.uuid4()),
            title="seq-test",
            description="sequence",
            role="backend",
            status=seq[0],
        )
        for next_status in seq[1:]:
            transition_task(task, next_status)
            assert task.status == next_status
