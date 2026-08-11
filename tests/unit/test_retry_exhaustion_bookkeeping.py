"""A task at its retry ceiling records its exhaustion once (#3628).

One observed 120-second run logged ``exhausted 2 retries -- recorded cross-run
failure in quarantine`` **1255 times** for a single seeded title. A budget of
two retries can be exhausted once; the count is the defect, not the log.

The tick loop re-offers every failed task on each pass, and the exhaustion
branch used to be a pure read: it recorded, warned, and returned ``False``
without marking the lineage as finished, so the next tick walked straight back
into it. These tests drive the state directly rather than reproducing the
intermittent run, and assert the recorded count against the budget -- the
invariant the log lines were a symptom of.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from bernstein.core.task_lifecycle import maybe_retry_task

from bernstein.core.security.quarantine import QUARANTINE_THRESHOLD, QuarantineStore
from bernstein.core.tasks.models import Complexity, Scope, Task, TaskStatus, TaskType

# Named explicitly so a future change to the default retry count fails here
# rather than silently widening the bound this test enforces.
_BUDGET = 2
_TICKS = 50


def _exhausted_task(*, task_id: str = "T-1", title: str = "Fix off-by-one in get_item route") -> Task:
    """A task whose retry_count already sits at its ceiling."""
    return Task(
        id=task_id,
        title=title,
        description="Seeded demo task.",
        role="backend",
        status=TaskStatus.FAILED,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
        retry_count=_BUDGET,
        max_retries=_BUDGET,
        estimated_minutes=10,
        model="sonnet",
        effort="high",
    )


def _offer(task: Task, retried: set[str], quarantine: QuarantineStore, workdir: Path) -> bool:
    return maybe_retry_task(
        task,
        retried_task_ids=retried,
        max_task_retries=_BUDGET,
        client=MagicMock(spec=httpx.Client),
        server_url="http://server",
        quarantine=quarantine,
        workdir=workdir,
        session_id=None,
    )


def _fail_count(quarantine: QuarantineStore, title: str) -> int:
    entry = quarantine.get_entry(title)
    return 0 if entry is None else entry.fail_count


def test_repeated_offers_record_the_exhaustion_once(tmp_path: Path) -> None:
    """The count, not the log volume, is what this pins."""
    quarantine = QuarantineStore(tmp_path / "quarantine.json")
    task = _exhausted_task()
    retried: set[str] = set()

    for _ in range(_TICKS):
        assert _offer(task, retried, quarantine, tmp_path) is False

    assert _fail_count(quarantine, task.title) == 1, (
        f"{_TICKS} offers of a task with a budget of {_BUDGET} recorded {_fail_count(quarantine, task.title)} failures"
    )


def test_the_recorded_count_never_exceeds_the_retry_budget(tmp_path: Path) -> None:
    """The invariant stated in the issue, independent of how it is enforced.

    ``is_quarantined`` alone cannot satisfy this: it only reports True once the
    stored count reaches ``QUARANTINE_THRESHOLD``, so a guard built on it would
    permit that many recordings before suppressing anything.
    """
    assert QUARANTINE_THRESHOLD > _BUDGET, (
        "this test is only meaningful while the quarantine threshold sits above the retry budget"
    )
    quarantine = QuarantineStore(tmp_path / "quarantine.json")
    task = _exhausted_task()
    retried: set[str] = set()

    for _ in range(_TICKS):
        _offer(task, retried, quarantine, tmp_path)

    assert _fail_count(quarantine, task.title) <= _BUDGET


def test_the_lineage_is_marked_finished_rather_than_re_entered(tmp_path: Path) -> None:
    """Enforced where the state lives: the same set that stops a second retry."""
    quarantine = QuarantineStore(tmp_path / "quarantine.json")
    task = _exhausted_task()
    retried: set[str] = set()

    _offer(task, retried, quarantine, tmp_path)

    assert task.id in retried, "an exhausted task must not be offered to the retry path again"


def test_a_second_task_is_unaffected_by_the_first(tmp_path: Path) -> None:
    """The suppression is per lineage, not a global latch."""
    quarantine = QuarantineStore(tmp_path / "quarantine.json")
    first = _exhausted_task(task_id="T-1", title="Fix off-by-one in get_item route")
    second = _exhausted_task(task_id="T-2", title="Add pagination to list_items")
    retried: set[str] = set()

    for _ in range(_TICKS):
        _offer(first, retried, quarantine, tmp_path)
    _offer(second, retried, quarantine, tmp_path)

    assert _fail_count(quarantine, first.title) == 1
    assert _fail_count(quarantine, second.title) == 1


@pytest.mark.parametrize("restarts", [2, 5])
def test_a_fresh_orchestrator_does_not_re_record_a_quarantined_task(tmp_path: Path, restarts: int) -> None:
    """A restart starts with an empty set, so the store is the second bound."""
    quarantine = QuarantineStore(tmp_path / "quarantine.json")
    task = _exhausted_task()

    for _ in range(restarts):
        # A new orchestrator process: the in-memory set does not survive it.
        retried: set[str] = set()
        for _ in range(_TICKS):
            _offer(task, retried, quarantine, tmp_path)

    assert _fail_count(quarantine, task.title) <= QUARANTINE_THRESHOLD, (
        "the quarantine store must bound what the in-memory set cannot"
    )
