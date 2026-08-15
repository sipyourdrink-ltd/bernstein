"""Tests for the ``plan.graph`` journal record (issue #3613).

The executed task graph is written to ``.sdd/runtime/task_graph.json``, an
overwritten file with no chain identity. ``_record_plan_graph_digest``
binds it to the run by appending its digest to the Merkle-chained event
journal.

Every assertion here reads the journal file back and inspects the rows.
Asserting that ``recorder.record`` was *called* would pass against a mock
whose method returns a truthy object, which is exactly the shape of bug
this record is supposed to make impossible.

The method is bound onto a minimal ``SimpleNamespace`` via
``types.MethodType`` so the real implementation runs against a real
``EventJournal`` without standing up a whole Orchestrator.
"""

from __future__ import annotations

import json
from types import MethodType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.orchestration.orchestrator import Orchestrator
from bernstein.core.orchestration.schedule_projection import (
    SCHEDULE_PROJECTION_REV,
    TaskNode,
    canonical_graph_digest,
)
from bernstein.core.replay.journal import EventJournal
from bernstein.core.tasks.models import Task

if TYPE_CHECKING:
    from pathlib import Path


def _task(task_id: str, *, role: str = "coder", depends_on: list[str] | None = None) -> Task:
    return Task(
        id=task_id,
        title=f"title for {task_id}",
        description=f"description for {task_id}",
        role=role,
        depends_on=list(depends_on or []),
    )


def _stub(recorder: Any) -> SimpleNamespace:
    """Bind the real method onto a stub carrying only what it touches."""
    stub = SimpleNamespace(_recorder=recorder, _last_graph_digest="")
    stub._record_plan_graph_digest = MethodType(
        Orchestrator._record_plan_graph_digest,  # type: ignore[arg-type]
        stub,
    )
    return stub


def _journal(tmp_path: Path, run_id: str = "run-1") -> EventJournal:
    return EventJournal(run_id=run_id, sdd_dir=tmp_path / ".sdd")


def _rows(journal: EventJournal, event: str = "plan.graph") -> list[dict[str, Any]]:
    """Read the journal file back and return the rows for *event*."""
    if not journal.path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with journal.path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("event") == event:
                rows.append(entry)
    return rows


def test_plan_graph_event_lands_in_the_journal_with_its_digest(tmp_path: Path) -> None:
    """The row exists on disk and carries the digest, rev and task count."""
    tasks = [_task("t1"), _task("t2", depends_on=["t1"])]
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_digest(tasks)

    rows = _rows(journal)
    assert len(rows) == 1
    row = rows[0]
    assert row["digest"] == canonical_graph_digest(
        [
            TaskNode(task_id="t1", role="coder", title="", description="", depends_on=()),
            TaskNode(task_id="t2", role="coder", title="", description="", depends_on=("t1",)),
        ]
    )
    assert row["rev"] == SCHEDULE_PROJECTION_REV
    assert row["task_count"] == 2
    # The row is chained like every other journal event. ``prev_hash`` is
    # the genesis sentinel here because this is the run's first append.
    assert row["event_hash"]
    assert "prev_hash" in row
    assert journal.verify().chain_consistent


def test_digest_is_produced_by_the_shared_canonical_encoder(tmp_path: Path) -> None:
    """Guards issue #3613's "no second encoder" constraint.

    The recorded digest must equal what the schedule projection's own
    canonical encoder produces for the same nodes. An ad-hoc encoding
    (``str(triples).encode()``, ``repr``, a bespoke JSON layout) lands on a
    different hex string and fails here, which is the point: the slice-2
    fold has to compare one definition of the graph, not two.
    """
    tasks = [_task("alpha", role="reviewer"), _task("beta", depends_on=["alpha"])]
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_digest(tasks)

    expected = canonical_graph_digest(
        [
            TaskNode(task_id="alpha", role="reviewer", title="", description="", depends_on=()),
            TaskNode(task_id="beta", role="coder", title="", description="", depends_on=("alpha",)),
        ]
    )
    assert _rows(journal)[0]["digest"] == expected


def test_plan_graph_digest_is_independent_of_task_iteration_order(tmp_path: Path) -> None:
    """``TaskGraph._tasks`` is keyed in list order; identity must not be.

    Two runs that iterate ``tasks_by_status`` differently record the same
    digest, so a reordered dict does not read as a replanned graph.
    """
    tasks = [
        _task("t1"),
        _task("t2", depends_on=["t1"]),
        _task("t3", role="reviewer", depends_on=["t2", "t1"]),
    ]

    forward_journal = _journal(tmp_path / "forward")
    _stub(forward_journal)._record_plan_graph_digest(tasks)

    reversed_journal = _journal(tmp_path / "reversed")
    _stub(reversed_journal)._record_plan_graph_digest(list(reversed(tasks)))

    assert _rows(forward_journal)[0]["digest"] == _rows(reversed_journal)[0]["digest"]


def test_depends_on_order_does_not_change_the_digest(tmp_path: Path) -> None:
    """``depends_on`` is a list; a reordered edge set is the same graph."""
    ab_journal = _journal(tmp_path / "ab")
    _stub(ab_journal)._record_plan_graph_digest([_task("a"), _task("b"), _task("c", depends_on=["a", "b"])])

    ba_journal = _journal(tmp_path / "ba")
    _stub(ba_journal)._record_plan_graph_digest([_task("a"), _task("b"), _task("c", depends_on=["b", "a"])])

    assert _rows(ab_journal)[0]["digest"] == _rows(ba_journal)[0]["digest"]


def test_unchanged_graph_appends_no_second_plan_graph_event(tmp_path: Path) -> None:
    """A 60-tick run whose graph never moves writes one row, not sixty."""
    tasks = [_task("t1"), _task("t2", depends_on=["t1"])]
    journal = _journal(tmp_path)
    stub = _stub(journal)

    for _ in range(60):
        stub._record_plan_graph_digest(tasks)

    assert len(_rows(journal)) == 1


def test_reworded_task_does_not_read_as_a_graph_change(tmp_path: Path) -> None:
    """Identity is the structural triple, not the prose."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_digest([_task("t1")])
    renamed = _task("t1")
    renamed.title = "a completely different title"
    renamed.description = "and a rewritten description"
    stub._record_plan_graph_digest([renamed])

    assert len(_rows(journal)) == 1


def test_graph_change_appends_exactly_one_new_event(tmp_path: Path) -> None:
    """A changed graph appends one row carrying the new digest."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    first = [_task("t1")]
    stub._record_plan_graph_digest(first)
    grown = [*first, _task("t2", depends_on=["t1"])]
    stub._record_plan_graph_digest(grown)

    rows = _rows(journal)
    assert len(rows) == 2
    assert rows[0]["digest"] != rows[1]["digest"]
    assert rows[0]["task_count"] == 1
    assert rows[1]["task_count"] == 2
    assert rows[1]["digest"] == canonical_graph_digest(
        [
            TaskNode(task_id="t1", role="coder", title="", description="", depends_on=()),
            TaskNode(task_id="t2", role="coder", title="", description="", depends_on=("t1",)),
        ]
    )


def test_a_changed_dependency_edge_alone_appends_a_new_event(tmp_path: Path) -> None:
    """Same tasks, different edges - the graph moved, so the digest moves."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_digest([_task("t1"), _task("t2")])
    stub._record_plan_graph_digest([_task("t1"), _task("t2", depends_on=["t1"])])

    rows = _rows(journal)
    assert len(rows) == 2
    assert rows[0]["digest"] != rows[1]["digest"]


def test_journal_write_failure_does_not_break_the_tick(tmp_path: Path) -> None:
    """The tick survives a recorder that raises, and retries next tick."""

    class _ExplodingRecorder:
        def __init__(self) -> None:
            self.calls = 0

        def record(self, event: str, **data: Any) -> None:
            self.calls += 1
            raise RuntimeError(f"journal is on fire: {event} {data}")

    recorder = _ExplodingRecorder()
    stub = _stub(recorder)
    tasks = [_task("t1")]

    stub._record_plan_graph_digest(tasks)

    assert recorder.calls == 1
    # A failed append must not be remembered as recorded, or the digest
    # would never reach the journal for the rest of the run.
    assert stub._last_graph_digest == ""

    stub._record_plan_graph_digest(tasks)
    assert recorder.calls == 2


def test_empty_graph_records_once_and_then_stays_quiet(tmp_path: Path) -> None:
    """An empty task list is a real graph state, recorded once."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_digest([])
    stub._record_plan_graph_digest([])

    rows = _rows(journal)
    assert len(rows) == 1
    assert rows[0]["task_count"] == 0


def test_journal_chain_verifies_after_plan_graph_events(tmp_path: Path) -> None:
    """The appended rows keep the Merkle chain intact."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_digest([_task("t1")])
    stub._record_plan_graph_digest([_task("t1"), _task("t2", depends_on=["t1"])])

    assert journal.verify().chain_consistent


@pytest.mark.parametrize("role", ["coder", "reviewer"])
def test_role_is_part_of_graph_identity(tmp_path: Path, role: str) -> None:
    """Two graphs differing only in a task's role are different graphs."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_digest([_task("t1", role="manager")])
    stub._record_plan_graph_digest([_task("t1", role=role)])

    rows = _rows(journal)
    assert len(rows) == 2
    assert rows[0]["digest"] != rows[1]["digest"]
