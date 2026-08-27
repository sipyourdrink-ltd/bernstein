"""Tests for the ``plan.graph.full`` journal record.

Mirrors :file:`test_plan_graph_digest.py` (issue #3613) but exercises the
full-graph row that carries ``goal`` plus every task's title/role/depends_on.

The method is bound onto a minimal ``SimpleNamespace`` via
``types.MethodType`` so the real implementation runs against a real
``EventJournal`` without standing up a whole Orchestrator.
"""

from __future__ import annotations

import json
from types import MethodType, SimpleNamespace
from typing import TYPE_CHECKING, Any

from bernstein.core.orchestration.orchestrator import Orchestrator
from bernstein.core.orchestration.schedule_projection import (
    SCHEDULE_PROJECTION_REV,
    TaskNode,
    canonical_graph_digest,
)
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.tasks.models import Task

if TYPE_CHECKING:
    from pathlib import Path


def _task(task_id: str, *, role: str = "coder", title: str = "", depends_on: list[str] | None = None) -> Task:
    return Task(
        id=task_id,
        title=title or f"title for {task_id}",
        description=f"description for {task_id}",
        role=role,
        depends_on=list(depends_on or []),
    )


def _stub(recorder: Any, goal: str = "") -> SimpleNamespace:
    """Bind the real method onto a stub carrying only what it touches."""
    stub = SimpleNamespace(_recorder=recorder, _goal=goal, _last_full_graph_digest="")
    stub._record_plan_graph_full = MethodType(
        Orchestrator._record_plan_graph_full,  # type: ignore[arg-type]
        stub,
    )
    return stub


def _journal(tmp_path: Path, run_id: str = "run-1") -> EventJournal:
    return EventJournal(run_id=run_id, sdd_dir=tmp_path / ".sdd")


def _rows(journal: EventJournal) -> list[dict[str, Any]]:
    """Read the journal file back and return the ``plan.graph.full`` rows."""
    if not journal.path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with journal.path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("event") == "plan.graph.full":
                rows.append(entry)
    return rows


def _expected_full_nodes(tasks: list[Task]) -> list[dict[str, Any]]:
    return [
        {
            "id": t.id,
            "role": t.role,
            "title": t.title,
            "depends_on": sorted(t.depends_on),
        }
        for t in tasks
    ]


def test_plan_graph_full_event_lands_in_the_journal(tmp_path: Path) -> None:
    """The row exists on disk and carries goal + nodes + digest + rev + task_count."""
    tasks = [
        _task("t1", role="manager", title="plan the job"),
        _task("t2", title="implement feature"),
    ]
    journal = _journal(tmp_path)
    stub = _stub(journal, goal="build a cli tool")

    stub._record_plan_graph_full(tasks)

    rows = _rows(journal)
    assert len(rows) == 1
    row = rows[0]
    assert row["goal"] == "build a cli tool"
    assert row["task_count"] == 2
    assert row["rev"] == SCHEDULE_PROJECTION_REV
    assert row["nodes"] == _expected_full_nodes(tasks)
    # Digest is the structural SHA-256 (title/description are NOT part of it).
    assert row["digest"] == canonical_graph_digest(
        [
            TaskNode(task_id="t1", role="manager", title="", description="", depends_on=()),
            TaskNode(task_id="t2", role="coder", title="", description="", depends_on=()),
        ]
    )
    assert row["event_hash"]
    assert "prev_hash" in row
    assert journal.verify().chain_consistent


def test_empty_goal_is_accepted(tmp_path: Path) -> None:
    """A run with no seed/goal still emits the event with ``goal`` absent."""
    tasks = [_task("t1")]
    journal = _journal(tmp_path)
    stub = _stub(journal, goal="")

    stub._record_plan_graph_full(tasks)

    rows = _rows(journal)
    assert len(rows) == 1
    # _fold_plan_graph_full requires a str goal; empty string is falsy and
    # the row stores whatever the caller passes, but the fold silently skips
    # an absent key. Here we store "" so the fold treats it as absent.
    # The event itself still lands in the journal.
    assert rows[0]["goal"] == ""


def test_nodes_are_sorted_by_task_id(tmp_path: Path) -> None:
    """Nodes in the row are ordered by task_id regardless of input order."""
    tasks = [
        _task("t3"),
        _task("t1"),
        _task("t2"),
    ]
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_full(tasks)

    row = _rows(journal)[0]
    ids = [n["id"] for n in row["nodes"]]
    assert ids == ["t1", "t2", "t3"]


def test_depends_on_is_sorted_per_node(tmp_path: Path) -> None:
    """``depends_on`` is sorted per node so the row is order-stable."""
    tasks = [_task("t3", depends_on=["t1", "t2"])]
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_full(tasks)

    row = _rows(journal)[0]
    assert row["nodes"][0]["depends_on"] == ["t1", "t2"]


def test_unchanged_graph_appends_no_second_full_event(tmp_path: Path) -> None:
    """A 60-tick run whose graph never moves writes one row, not sixty."""
    tasks = [_task("t1"), _task("t2", depends_on=["t1"])]
    journal = _journal(tmp_path)
    stub = _stub(journal)

    for _ in range(60):
        stub._record_plan_graph_full(tasks)

    assert len(_rows(journal)) == 1


def test_reworded_task_does_not_trigger_a_new_full_event(tmp_path: Path) -> None:
    """Identity is the structural triple; a reworded title is a no-op for dedup."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_full([_task("t1", title="initial title")])
    renamed = _task("t1", title="a completely different title")
    renamed.description = "and a rewritten description"
    stub._record_plan_graph_full([renamed])

    assert len(_rows(journal)) == 1


def test_graph_change_appends_exactly_one_new_full_event(tmp_path: Path) -> None:
    """A changed graph appends one row carrying the new digest."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    first = [_task("t1")]
    stub._record_plan_graph_full(first)
    grown = [*first, _task("t2", depends_on=["t1"])]
    stub._record_plan_graph_full(grown)

    rows = _rows(journal)
    assert len(rows) == 2
    assert rows[0]["digest"] != rows[1]["digest"]
    assert rows[0]["task_count"] == 1
    assert rows[1]["task_count"] == 2


def test_a_changed_dependency_edge_alone_appends_a_new_full_event(tmp_path: Path) -> None:
    """Same tasks, different edges - the graph moved, so the digest moves."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_full([_task("t1"), _task("t2")])
    stub._record_plan_graph_full([_task("t1"), _task("t2", depends_on=["t1"])])

    rows = _rows(journal)
    assert len(rows) == 2
    assert rows[0]["digest"] != rows[1]["digest"]


def test_empty_graph_records_once_and_then_stays_quiet(tmp_path: Path) -> None:
    """An empty task list is a real graph state, recorded once."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_full([])
    stub._record_plan_graph_full([])

    rows = _rows(journal)
    assert len(rows) == 1
    assert rows[0]["task_count"] == 0
    assert rows[0]["nodes"] == []


def test_journal_chain_verifies_after_plan_graph_full_events(tmp_path: Path) -> None:
    """The appended rows keep the Merkle chain intact."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_full([_task("t1")])
    stub._record_plan_graph_full([_task("t1"), _task("t2", depends_on=["t1"])])

    assert journal.verify().chain_consistent


def test_digest_matches_plan_graph_digest_for_same_nodes(tmp_path: Path) -> None:
    """``plan.graph.full`` reuses the same canonical encoder as ``plan.graph``."""
    tasks = [_task("alpha", role="reviewer"), _task("beta", depends_on=["alpha"])]
    full_journal = _journal(tmp_path / "full")
    stub_full = _stub(full_journal)
    stub_full._record_plan_graph_full(tasks)

    digest_journal = _journal(tmp_path / "digest")
    stub_digest = SimpleNamespace(_recorder=digest_journal, _last_graph_digest="")
    stub_digest._record_plan_graph_digest = MethodType(
        Orchestrator._record_plan_graph_digest,  # type: ignore[arg-type]
        stub_digest,
    )
    stub_digest._record_plan_graph_digest(tasks)

    full_rows = _rows(full_journal)
    digest_rows = [r for r in load_events(digest_journal.path).events if r.get("event") == "plan.graph"]
    assert len(full_rows) == 1
    assert len(digest_rows) == 1
    assert full_rows[0]["digest"] == digest_rows[0]["digest"]


def test_journal_write_failure_does_not_break_tick(tmp_path: Path) -> None:
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

    stub._record_plan_graph_full(tasks)

    assert recorder.calls == 1
    # A failed append must not be remembered as recorded, or the digest
    # would never reach the journal for the rest of the run.
    assert stub._last_full_graph_digest == ""

    stub._record_plan_graph_full(tasks)
    assert recorder.calls == 2


def test_role_is_part_of_graph_identity(tmp_path: Path) -> None:
    """Two graphs differing only in a task's role are different graphs."""
    journal = _journal(tmp_path)
    stub = _stub(journal)

    stub._record_plan_graph_full([_task("t1", role="manager")])
    stub._record_plan_graph_full([_task("t1", role="coder")])

    rows = _rows(journal)
    assert len(rows) == 2
    assert rows[0]["digest"] != rows[1]["digest"]
    assert rows[0]["nodes"][0]["role"] == "manager"
    assert rows[1]["nodes"][0]["role"] == "coder"
