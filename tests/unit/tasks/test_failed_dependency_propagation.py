"""A failed task tells its dependents (#3452).

Two halves are exercised here: the pure ``unreachable_tasks`` projection, and
the live task store, where a stranded dependent must reach a terminal,
operator-visible status instead of being re-queued every tick forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.core.tasks.lifecycle import (
    IN_FLIGHT_TASK_STATUSES,
    SUCCESSFUL_TASK_STATUSES,
    TASK_TRANSITIONS,
    UNSUCCESSFUL_TERMINAL_STATUSES,
)
from bernstein.core.tasks.models import Task, TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore
from bernstein.core.tasks.unreachable import unreachable_tasks

UNSUCCESSFUL_DEPENDENCY_STATUSES = [
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.REFUSED,
    TaskStatus.ORPHANED,
]


def _task(task_id: str, status: TaskStatus, depends_on: list[str] | None = None) -> Task:
    return Task(
        id=task_id,
        title=task_id,
        description="d",
        role="backend",
        status=status,
        depends_on=list(depends_on or []),
    )


# ---------------------------------------------------------------------------
# The projection: a function of the graph, not of traversal order
# ---------------------------------------------------------------------------


class TestUnreachableProjection:
    def test_set_is_a_function_of_the_graph_not_insertion_order(self) -> None:
        # A fails; B depends on A; C depends on B.
        a = _task("A", TaskStatus.FAILED)
        b = _task("B", TaskStatus.OPEN, ["A"])
        c = _task("C", TaskStatus.OPEN, ["B"])
        expected = [("B", "A"), ("C", "B")]
        for order in ([a, b, c], [c, b, a], [b, c, a]):
            assert unreachable_tasks(order) == expected

    def test_transitive_dependent_names_its_nearest_dependency_as_cause(self) -> None:
        tasks = [
            _task("A", TaskStatus.FAILED),
            _task("B", TaskStatus.OPEN, ["A"]),
            _task("C", TaskStatus.OPEN, ["B"]),
        ]
        causes = dict(unreachable_tasks(tasks))
        assert causes["C"] == "B", "a transitive strand must name the link that stranded it, not the root failure"

    def test_several_stranded_dependencies_report_the_lowest_id_stably(self) -> None:
        tasks = [
            _task("A", TaskStatus.FAILED),
            _task("Z", TaskStatus.CANCELLED),
            _task("B", TaskStatus.OPEN, ["Z", "A"]),
        ]
        assert dict(unreachable_tasks(tasks))["B"] == "A"
        # Same graph, dependency edges declared the other way round.
        tasks[2] = _task("B", TaskStatus.OPEN, ["A", "Z"])
        assert dict(unreachable_tasks(tasks))["B"] == "A"

    def test_a_cause_stranded_only_on_a_later_pass_still_wins_by_id(self) -> None:
        # "AA" is stranded transitively via "M"; "ZZ" is a root failure. The
        # cause must be "AA" (lowest id) even though it only enters the set on
        # the second fixpoint pass.
        tasks = [
            _task("M", TaskStatus.FAILED),
            _task("AA", TaskStatus.OPEN, ["M"]),
            _task("ZZ", TaskStatus.FAILED),
            _task("T", TaskStatus.OPEN, ["ZZ", "AA"]),
        ]
        assert dict(unreachable_tasks(tasks))["T"] == "AA"

    def test_successful_dependency_leaves_the_set_empty(self) -> None:
        for done in (TaskStatus.DONE, TaskStatus.CLOSED):
            tasks = [
                _task("A", done),
                _task("B", TaskStatus.OPEN, ["A"]),
                _task("C", TaskStatus.OPEN, ["B"]),
            ]
            assert unreachable_tasks(tasks) == []

    @pytest.mark.parametrize("status", UNSUCCESSFUL_DEPENDENCY_STATUSES)
    def test_every_unsuccessful_terminal_status_strands_its_dependents(self, status: TaskStatus) -> None:
        tasks = [_task("A", status), _task("B", TaskStatus.OPEN, ["A"])]
        assert unreachable_tasks(tasks) == [("B", "A")]

    def test_one_stranded_dependency_strands_a_task_with_a_live_one_too(self) -> None:
        tasks = [
            _task("A", TaskStatus.FAILED),
            _task("B", TaskStatus.IN_PROGRESS),
            _task("C", TaskStatus.OPEN, ["A", "B"]),
        ]
        assert unreachable_tasks(tasks) == [("C", "A")]

    def test_a_task_that_ended_on_its_own_is_a_cause_not_a_casualty(self) -> None:
        # B failed while running; it is the root of C's strand, not a member.
        tasks = [
            _task("A", TaskStatus.FAILED),
            _task("B", TaskStatus.FAILED, ["A"]),
            _task("C", TaskStatus.OPEN, ["B"]),
        ]
        assert unreachable_tasks(tasks) == [("C", "B")]

    def test_unknown_dependency_id_does_not_strand(self) -> None:
        # A dangling edge is a different defect; the projection stays quiet.
        assert unreachable_tasks([_task("B", TaskStatus.OPEN, ["missing"])]) == []


# ---------------------------------------------------------------------------
# The classification the projection is derived from
# ---------------------------------------------------------------------------


class TestStatusClassification:
    def test_every_task_status_is_classified_exactly_once(self) -> None:
        # A newly added TaskStatus must be classified, or the projection would
        # silently treat it as in-flight and strand nothing.
        buckets = (SUCCESSFUL_TASK_STATUSES, UNSUCCESSFUL_TERMINAL_STATUSES, IN_FLIGHT_TASK_STATUSES)
        union = set().union(*buckets)
        assert union == set(TaskStatus)
        assert sum(len(bucket) for bucket in buckets) == len(union)

    def test_blocked_by_failed_dep_has_the_same_recovery_edges_as_blocked_by_abandon(self) -> None:
        for target in (TaskStatus.OPEN, TaskStatus.CANCELLED, TaskStatus.ABANDONED):
            assert (TaskStatus.BLOCKED_BY_FAILED_DEP, target) in TASK_TRANSITIONS

    def test_blocked_by_failed_dep_cannot_reach_a_successful_status(self) -> None:
        for target in (TaskStatus.DONE, TaskStatus.CLOSED):
            assert (TaskStatus.BLOCKED_BY_FAILED_DEP, target) not in TASK_TRANSITIONS


# ---------------------------------------------------------------------------
# The live task store
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> TaskStore:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return TaskStore(runtime / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")


async def _insert(store: TaskStore, task_id: str, **overrides: Any) -> Task:
    base: dict[str, Any] = {
        "id": task_id,
        "title": task_id,
        "description": "d",
        "role": "backend",
        "status": TaskStatus.OPEN,
    }
    base.update(overrides)
    task = Task(**base)
    store._tasks[task.id] = task  # type: ignore[attr-defined]
    store._index_add(task)  # type: ignore[attr-defined]
    return task


@pytest.mark.asyncio
class TestTaskStoreFailurePropagation:
    async def test_dependent_of_a_failed_task_reaches_a_terminal_blocked_state(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _insert(store, "A", status=TaskStatus.IN_PROGRESS)
        dependent = await _insert(store, "B", depends_on=["A"])
        await store.fail("A", "boom")
        assert dependent.status is TaskStatus.BLOCKED_BY_FAILED_DEP

    async def test_transitive_dependent_of_a_failed_task_reaches_it_too(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _insert(store, "A", status=TaskStatus.IN_PROGRESS)
        b = await _insert(store, "B", depends_on=["A"])
        c = await _insert(store, "C", depends_on=["B"])
        await store.fail("A", "boom")
        assert b.status is TaskStatus.BLOCKED_BY_FAILED_DEP
        assert c.status is TaskStatus.BLOCKED_BY_FAILED_DEP

    async def test_blocked_dependent_records_the_causing_task_id(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _insert(store, "A", status=TaskStatus.IN_PROGRESS)
        b = await _insert(store, "B", depends_on=["A"])
        c = await _insert(store, "C", depends_on=["B"])
        await store.fail("A", "boom")
        assert b.metadata["blocking_task_id"] == "A"
        # The nearest link, so the chain is walkable one hop at a time.
        assert c.metadata["blocking_task_id"] == "B"
        assert "A" in (b.result_summary or "")

    async def test_dependent_of_a_failed_task_is_not_requeued_on_later_ticks(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # The dependency reaches FAILED without going through fail(), which is
        # how a crash-recovery or restored-journal row arrives: only the
        # derived check in the claim path can catch this one.
        await _insert(store, "A", status=TaskStatus.FAILED)
        dependent = await _insert(store, "B", depends_on=["A"])
        assert await store.claim_next("backend") is None
        assert dependent.status is TaskStatus.BLOCKED_BY_FAILED_DEP
        # The defect was a heap slot burned every tick, forever.
        queue = store._priority_queues.get(("backend", TaskStatus.OPEN), [])  # type: ignore[attr-defined]
        assert queue == []
        assert await store.claim_next("backend") is None
        assert queue == []

    @pytest.mark.parametrize("status", UNSUCCESSFUL_DEPENDENCY_STATUSES)
    async def test_each_unsuccessful_dependency_status_blocks_its_dependent(
        self, tmp_path: Path, status: TaskStatus
    ) -> None:
        store = _store(tmp_path)
        await _insert(store, "A", status=status)
        dependent = await _insert(store, "B", depends_on=["A"])
        await store.claim_next("backend")
        assert dependent.status is TaskStatus.BLOCKED_BY_FAILED_DEP

    async def test_cancel_propagates_to_dependents(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _insert(store, "A", status=TaskStatus.IN_PROGRESS)
        dependent = await _insert(store, "B", depends_on=["A"])
        await store.cancel("A", "operator stopped it")
        assert dependent.status is TaskStatus.BLOCKED_BY_FAILED_DEP

    async def test_cancel_cascade_propagates_to_an_in_progress_dependent(self, tmp_path: Path) -> None:
        """#4247: the cascade transitions inline, so it used to skip propagation.

        An already-CLAIMED or IN_PROGRESS dependent is the case that matters:
        it never passes through ``claim_next`` again, so the backstop there
        cannot rescue it and it would keep running against a dependency that
        can never complete.
        """
        store = _store(tmp_path)
        await _insert(store, "P", status=TaskStatus.IN_PROGRESS)
        await _insert(store, "S", status=TaskStatus.IN_PROGRESS, parent_task_id="P")
        dependent = await _insert(store, "D", status=TaskStatus.IN_PROGRESS, depends_on=["S"])

        await store.cancel_cascade("P", "operator stopped the tree")

        assert dependent.status is TaskStatus.BLOCKED_BY_FAILED_DEP
        assert dependent.metadata["blocking_task_id"] == "S"

    async def test_cancel_cascade_propagates_transitively(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _insert(store, "P", status=TaskStatus.IN_PROGRESS)
        d1 = await _insert(store, "D1", status=TaskStatus.IN_PROGRESS, depends_on=["P"])
        d2 = await _insert(store, "D2", depends_on=["D1"])

        await store.cancel_cascade("P", "operator stopped the tree")

        assert d1.status is TaskStatus.BLOCKED_BY_FAILED_DEP
        assert d2.status is TaskStatus.BLOCKED_BY_FAILED_DEP
        assert d2.metadata["blocking_task_id"] == "D1"

    async def test_cancel_cascade_still_cancels_a_descendant_that_also_depends_on_the_root(
        self, tmp_path: Path
    ) -> None:
        """A subtask must end CANCELLED, not BLOCKED_BY_FAILED_DEP.

        Propagation runs after the whole subtree is cancelled for exactly this
        case: marking the dependent first would leave it in a status the
        cascade's ``cancellable`` set does not cover, and it would never be
        cancelled at all.
        """
        store = _store(tmp_path)
        await _insert(store, "P", status=TaskStatus.IN_PROGRESS)
        both = await _insert(store, "S", status=TaskStatus.IN_PROGRESS, parent_task_id="P", depends_on=["P"])

        await store.cancel_cascade("P", "operator stopped the tree")

        assert both.status is TaskStatus.CANCELLED

    async def test_cancel_cascade_leaves_an_unrelated_task_alone(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _insert(store, "P", status=TaskStatus.IN_PROGRESS)
        untouched = await _insert(store, "U", status=TaskStatus.IN_PROGRESS)

        await store.cancel_cascade("P", "operator stopped the tree")

        assert untouched.status is TaskStatus.IN_PROGRESS

    async def test_a_live_sibling_dependency_does_not_keep_the_dependent_claimable(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _insert(store, "A", status=TaskStatus.IN_PROGRESS)
        await _insert(store, "B", status=TaskStatus.IN_PROGRESS)
        dependent = await _insert(store, "C", depends_on=["A", "B"])
        await store.fail("A", "boom")
        assert dependent.status is TaskStatus.BLOCKED_BY_FAILED_DEP
        assert await store.claim_next("backend") is None

    async def test_a_satisfied_dependency_still_yields_a_claim(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _insert(store, "A", status=TaskStatus.DONE)
        await _insert(store, "B", depends_on=["A"])
        claimed = await store.claim_next("backend")
        assert claimed is not None
        assert claimed.id == "B"

    async def test_abandon_cascade_still_uses_blocked_by_abandon(self, tmp_path: Path) -> None:
        # The two states must stay distinguishable in the record.
        store = _store(tmp_path)
        await _insert(store, "A", status=TaskStatus.IN_PROGRESS)
        dependent = await _insert(store, "B", depends_on=["A"])
        await store.abandon("A", "out_of_scope", "not doable")
        assert dependent.status is TaskStatus.BLOCKED_BY_ABANDON

    async def test_status_summary_reports_the_unreachable_set_with_causes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _insert(store, "A", status=TaskStatus.IN_PROGRESS)
        await _insert(store, "B", depends_on=["A"])
        await _insert(store, "C", depends_on=["B"])
        await store.fail("A", "boom")
        summary = store.status_summary()
        assert summary["unreachable"] == [
            {"task_id": "B", "blocked_by": "A"},
            {"task_id": "C", "blocked_by": "B"},
        ]

    async def test_status_summary_unreachable_is_empty_on_a_healthy_graph(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await _insert(store, "A", status=TaskStatus.DONE)
        await _insert(store, "B", depends_on=["A"])
        assert store.status_summary()["unreachable"] == []
