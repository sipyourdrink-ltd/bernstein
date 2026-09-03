"""A retry succeeds under a NEW task id; its dependents must still unblock.

Regression for finding L (2026-09-03): retry `2d996831f7f2` reached done while
judge-1, phase-2 and fix-1 kept `depends_on: [4e86bcefa22a]` - the failed
original - and logged `remains blocked` for the rest of the run. The store's
claim check and blocking_dependency already resolved lineage; the
orchestrator's own readiness filter carried a raw id set and did not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bernstein.core.tasks.models import Task, TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore
from bernstein.core.tasks.unreachable import blocking_dependency, satisfied_dependency_ids


def _task(tid: str, status: str, *, deps: list[str] | None = None, meta: dict | None = None) -> Task:
    return Task.from_dict(
        {
            "id": tid,
            "title": tid,
            "description": "d",
            "role": "resolver",
            "status": status,
            "depends_on": deps or [],
            "metadata": meta or {},
        }
    )


def test_a_done_retry_satisfies_the_original_id() -> None:
    retry = _task("2d996831f7f2", "done", meta={"original_task_id": "4e86bcefa22a", "retry_of": "4e86bcefa22a"})
    ids = satisfied_dependency_ids([retry])
    assert "4e86bcefa22a" in ids, "the retry must satisfy the edge naming the original"
    assert "2d996831f7f2" in ids


def test_a_plain_done_task_satisfies_only_itself() -> None:
    assert satisfied_dependency_ids([_task("a", "done")]) == {"a"}


def test_non_string_lineage_is_ignored() -> None:
    bad = _task("r", "done", meta={"original_task_id": None, "retry_of": ""})
    assert satisfied_dependency_ids([bad]) == {"r"}


def test_the_readiness_filter_lets_the_dependent_through() -> None:
    """The exact expression the orchestrator's open-task filter evaluates."""
    failed = _task("4e86bcefa22a", "failed")
    retry = _task("2d996831f7f2", "done", meta={"original_task_id": "4e86bcefa22a", "retry_of": "4e86bcefa22a"})
    judge = _task("372cf846f93e", "open", deps=["4e86bcefa22a"])

    done_ids = satisfied_dependency_ids([retry])
    assert all(dep in done_ids for dep in judge.depends_on), "judge-1 must become ready"

    # And it must not be reported as stranded by the failed original either.
    tasks = {t.id: t for t in (failed, retry, judge)}
    assert blocking_dependency(judge, tasks) is None


def test_without_the_retry_the_dependent_stays_blocked() -> None:
    """The guard is lineage, not 'ignore failed deps'."""
    failed = _task("4e86bcefa22a", "failed")
    judge = _task("372cf846f93e", "open", deps=["4e86bcefa22a"])
    assert not all(dep in satisfied_dependency_ids([]) for dep in judge.depends_on)
    assert blocking_dependency(judge, {failed.id: failed, judge.id: judge}) == "4e86bcefa22a"


# ---------------------------------------------------------------------------
# The live task store: A fails, retries under a new id, B must become claimable
# ---------------------------------------------------------------------------


async def _insert(store: Any, task_id: str, **overrides: Any) -> Task:
    base: dict[str, Any] = {
        "id": task_id,
        "title": task_id,
        "description": "d",
        "role": "resolver",
        "status": TaskStatus.OPEN,
    }
    base.update(overrides)
    task = Task(**base)
    store._tasks[task.id] = task
    store._index_add(task)
    return task


@pytest.mark.asyncio
async def test_b_unblocks_once_a_s_retry_succeeds(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    store = TaskStore(runtime / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")

    await _insert(store, "A", status=TaskStatus.IN_PROGRESS)
    b = await _insert(store, "B", depends_on=["A"])

    await store.fail("A", "boom")
    assert b.status is TaskStatus.BLOCKED_BY_FAILED_DEP
    assert store._dependencies_satisfied(b) is False

    # The retry: a NEW id, B's edge still names "A".
    retry = await _insert(
        store,
        "A-retry",
        status=TaskStatus.IN_PROGRESS,
        metadata={"original_task_id": "A", "retry_of": "A"},
    )
    await store.complete(retry.id, "done")

    assert store._dependencies_satisfied(b) is True, "a successful retry must satisfy B's edge on A"
    assert b.status is not TaskStatus.BLOCKED_BY_FAILED_DEP, "B must be unblocked, not stranded"


# ---------------------------------------------------------------------------
# The diagnostic validator must not report a satisfied edge as stuck
# ---------------------------------------------------------------------------


def test_validator_does_not_report_a_retried_dependency_as_stuck() -> None:
    """`depends on X which is failed - task remains blocked` for a running dependent."""
    from bernstein.core.quality.dep_validator import DependencyValidator

    failed = _task("4e86bcefa22a", "failed")
    retry = _task("2d996831f7f2", "done", meta={"original_task_id": "4e86bcefa22a", "retry_of": "4e86bcefa22a"})
    judge = _task("372cf846f93e", "open", deps=["4e86bcefa22a"])

    result = DependencyValidator().validate([failed, retry, judge])
    assert result.stuck_deps == [], "the retry satisfies the edge; nothing is stuck"


def test_validator_still_reports_a_genuinely_failed_dependency() -> None:
    from bernstein.core.quality.dep_validator import DependencyValidator

    failed = _task("A", "failed")
    judge = _task("B", "open", deps=["A"])

    result = DependencyValidator().validate([failed, judge])
    assert result.stuck_deps == [("B", "A", "failed")]


def test_validator_is_quiet_while_the_retry_is_still_in_flight() -> None:
    """The engine is re-attempting the dependency; the dependent is not wedged."""
    from bernstein.core.quality.dep_validator import DependencyValidator

    failed = _task("A", "failed")
    in_flight = _task("A-retry", "in_progress", meta={"original_task_id": "A", "retry_of": "A"})
    dependent = _task("B", "open", deps=["A"])

    result = DependencyValidator().validate([failed, in_flight, dependent])
    assert result.stuck_deps == []


def test_validator_reports_it_again_once_every_retry_has_failed() -> None:
    from bernstein.core.quality.dep_validator import DependencyValidator

    failed = _task("A", "failed")
    dead_retry = _task("A-retry", "failed", meta={"original_task_id": "A", "retry_of": "A"})
    dependent = _task("B", "open", deps=["A"])

    result = DependencyValidator().validate([failed, dead_retry, dependent])
    assert result.stuck_deps == [("B", "A", "failed")]
