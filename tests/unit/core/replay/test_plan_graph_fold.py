"""The structural fold agrees with real graph producers across journal prefixes."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bernstein.core.orchestration.orchestrator import Orchestrator
from bernstein.core.orchestration.schedule_projection import TaskNode, canonical_graph_bytes
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.tasks.models import Task

if TYPE_CHECKING:
    from bernstein.core.replay.plan_graph import PlanGraphResult

MODULE = "bernstein.core.replay.plan_graph"


def _fold(rows: list[dict[str, Any]]) -> PlanGraphResult:
    # Keep missing implementation a meaningful assertion failure, not a collection error.
    assert importlib.util.find_spec(MODULE) is not None, "structural plan graph fold is missing"
    return importlib.import_module(MODULE).fold_plan_graph(rows)


@st.composite
def _graphs(draw: st.DrawFn) -> list[Task]:
    ids = draw(st.lists(st.sampled_from(["a", "b", "c", "é", "東京"]), unique=True, max_size=5))
    return [
        Task(
            id=task_id,
            role=draw(st.sampled_from(["coder", "reviewer", "", "מנהל"])),
            title=draw(st.text(max_size=12)),
            description="not structural",
            # Dangling, repeated, cyclic and self edges are all producer-valid.
            depends_on=draw(st.lists(st.sampled_from(["a", "b", "c", "missing", task_id]), max_size=5)),
        )
        for task_id in ids
    ]


def _task(*, id: str, title: str, depends_on: list[str] | None = None) -> Task:
    return Task(id=id, title=title, description="", role="coder", depends_on=depends_on or [])


def _bytes(tasks: list[Task]) -> bytes:
    return canonical_graph_bytes(TaskNode(t.id, t.role, "", "", tuple(t.depends_on)) for t in tasks)


class _Recorder:
    def __init__(self, journal: EventJournal) -> None:
        self.journal = journal
        self.fail_digest = False
        self.fail_full = False

    def record(self, event: str, **data: Any) -> None:
        if (event == "plan.graph" and self.fail_digest) or (event == "plan.graph.full" and self.fail_full):
            raise OSError("generated append failure")
        self.journal.record(event, **data)


def _produce(ticks: list[tuple[list[Task], bool, bool]], root: Path) -> list[dict[str, Any]]:
    journal = EventJournal(run_id="graph-fold", sdd_dir=root / ".sdd")
    recorder = _Recorder(journal)
    stub = SimpleNamespace(_recorder=recorder, _goal="labels only", _last_graph_digest="", _last_full_graph_digest="")
    for tasks, fail_digest, fail_full in ticks:
        journal.record("tick_start")
        recorder.fail_digest, recorder.fail_full = fail_digest, fail_full
        Orchestrator._record_plan_graph_digest(stub, tasks)
        Orchestrator._record_plan_graph_full(stub, tasks)
    assert journal.verify().chain_consistent
    return load_events(journal.path).events


def test_replay_package_exposes_structural_fold(tmp_path: Path) -> None:
    from bernstein.core import replay

    tasks = [_task(id="a", title="A"), _task(id="b", title="B", depends_on=["a"])]
    rows = _produce([(tasks, False, False)], tmp_path)
    assert "fold_plan_graph" in replay.__all__
    assert "PlanGraphResult" in replay.__all__
    result = replay.fold_plan_graph(rows)
    assert isinstance(result, replay.PlanGraphResult)
    assert result.status == "available"
    assert result.canonical_bytes == _bytes(tasks)


@settings(max_examples=40, deadline=None)
@given(_graphs())
def test_generated_producer_graph_reproduces_exact_structural_bytes(tasks: list[Task]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        rows = _produce([(tasks, False, False)], Path(directory))
    before = copy.deepcopy(rows)
    result = _fold(rows)
    assert result.status == "available"
    assert result.canonical_bytes == canonical_graph_bytes(result.nodes) == _bytes(tasks)
    assert result.recorded_digest == result.recomputed_digest == rows[-1]["digest"]
    assert hashlib.sha256(result.canonical_bytes).hexdigest() == rows[-1]["digest"]
    assert all(n.title == n.description == "" and n.metadata == () for n in result.nodes)
    assert rows == before


@settings(max_examples=35, deadline=None)
@given(st.lists(st.tuples(_graphs(), st.booleans(), st.booleans()), min_size=1, max_size=8))
def test_generated_append_failures_and_every_prefix_are_total(ticks: list[tuple[list[Task], bool, bool]]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        rows = _produce(ticks, Path(directory))
    expected = {hashlib.sha256(_bytes(tasks)).hexdigest(): _bytes(tasks) for tasks, _, _ in ticks}
    before = copy.deepcopy(rows)
    for end in range(len(rows) + 1):
        prefix = rows[:end]
        result = _fold(prefix)
        assert result == _fold(prefix)
        graph_rows = [r for r in prefix if r["event"] in {"plan.graph", "plan.graph.full"}]
        if not graph_rows:
            assert result.status == "empty"
        elif result.status == "available":
            assert result.recorded_digest == graph_rows[-1]["digest"]
            assert result.canonical_bytes == expected[result.recorded_digest]
        else:
            assert result.status == "unavailable"
            assert graph_rows[-1]["event"] == "plan.graph"
            assert result.nodes == () and result.canonical_bytes is None
        if graph_rows and graph_rows[-1]["event"] == "plan.graph.full":
            assert result.status == "available"
    assert rows == before


@settings(max_examples=5, deadline=None)
@given(st.lists(st.tuples(_graphs(), st.booleans(), st.booleans()), min_size=1, max_size=4))
def test_generated_prefixes_match_across_real_hash_seed_processes(ticks: list[tuple[list[Task], bool, bool]]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        rows = _produce(ticks, Path(directory))
    _fold(rows)  # Meaningful red assertion before spawning children.
    script = """
import json, sys
from bernstein.core.replay.plan_graph import fold_plan_graph
rows = json.load(sys.stdin)
outputs = []
for end in range(len(rows) + 1):
    result = fold_plan_graph(rows[:end])
    outputs.append([result.status, result.recorded_digest, result.recomputed_digest,
                    result.reason, None if result.canonical_bytes is None else result.canonical_bytes.hex()])
print(json.dumps(outputs, sort_keys=True, separators=(",", ":")))
"""
    outputs = [
        subprocess.run(
            [sys.executable, "-c", script],
            input=json.dumps(rows).encode(),
            capture_output=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("1", "987654")
    ]
    assert outputs[0] == outputs[1]


def test_missing_snapshot_hides_stale_nodes_and_full_only_recovers(tmp_path: Path) -> None:
    a, b = [_task(id="a", title="A")], [_task(id="b", title="B")]
    rows = _produce([(a, False, False), (b, False, True)], tmp_path)
    stale = _fold(rows)
    assert stale.status == "unavailable" and stale.nodes == () and stale.canonical_bytes is None
    assert stale.recorded_digest == hashlib.sha256(_bytes(b)).hexdigest()
    assert stale.recomputed_digest is None
    rows = _produce([(a, False, False), (b, True, False)], tmp_path / "full-only")
    assert _fold(rows).canonical_bytes == _bytes(b)
    assert _fold(_produce([(b, True, False)], tmp_path / "only")).canonical_bytes == _bytes(b)
    assert _fold(_produce([(b, False, True)], tmp_path / "marker")).status == "unavailable"


def test_delayed_marker_for_latest_full_snapshot_remains_available(tmp_path: Path) -> None:
    tasks = [_task(id="a", title="A")]
    rows = _produce([(tasks, True, False), (tasks, False, False)], tmp_path)
    assert rows[-1]["event"] == "plan.graph"
    assert _fold(rows).canonical_bytes == _bytes(tasks)


def test_order_labels_and_unrelated_events_do_not_change_structural_result(tmp_path: Path) -> None:
    tasks = [_task(id="b", title="B", depends_on=["z", "a"]), _task(id="a", title="A")]
    rows = _produce([(tasks, False, False)], tmp_path)
    expected = _fold(rows)
    altered = copy.deepcopy(rows)
    altered[-1]["nodes"].reverse()
    for node in altered[-1]["nodes"]:
        node["depends_on"].reverse()
        node["title"] = {"ignored": "label"}
    altered[-1]["goal"] = "other goal"
    altered[-1]["ts"] = "other time"
    altered.extend([{"event": "task_completed", "task_id": "not-membership"}, {"event": "future-event"}])
    assert _fold(altered) == expected


@pytest.mark.parametrize(
    "change",
    [
        {"rev": "future"},
        {"rev": 1},
        {"digest": "0" * 64},
        {"digest": "bad"},
        {"digest": None},
        {"task_count": True},
        {"task_count": -1},
        {"task_count": 2},
        {"nodes": None},
        {"nodes": [{}]},
        {"nodes": [None]},
        {"nodes": [{"id": "a", "role": "coder", "depends_on": "a"}]},
        {"nodes": [{"id": "a", "role": 4, "depends_on": []}]},
        {"nodes": [{"id": 4, "role": "coder", "depends_on": []}]},
        {"nodes": [{"id": "a", "role": "coder", "depends_on": [4]}]},
    ],
)
def test_malformed_full_rows_fail_closed_even_before_later_valid_row(tmp_path: Path, change: dict[str, Any]) -> None:
    rows = _produce([([_task(id="a", title="A")], False, False)], tmp_path)
    malformed = {**rows[-1], **change}
    result = _fold([*rows, malformed, rows[-1]])
    assert result.status == "invalid"
    assert result.nodes == () and result.canonical_bytes is None and result.reason


@pytest.mark.parametrize("change", [{"rev": "future"}, {"digest": "bad"}, {"task_count": False}, {"task_count": -1}])
def test_malformed_markers_fail_closed(tmp_path: Path, change: dict[str, Any]) -> None:
    rows = _produce([([], False, False)], tmp_path)
    result = _fold([{**rows[-2], **change}, rows[-1]])
    assert result.status == "invalid" and result.nodes == () and result.canonical_bytes is None


def test_no_graph_and_recorded_empty_graph_are_distinct(tmp_path: Path) -> None:
    assert _fold([]).status == "empty"
    assert _fold([{"event": "task_completed", "task_id": "a"}]).nodes == ()
    result = _fold(_produce([([], False, False)], tmp_path))
    assert result.status == "available" and result.nodes == ()
    assert result.canonical_bytes == _bytes([])


@pytest.mark.parametrize("marker_first", [False, True])
def test_same_digest_cannot_claim_conflicting_counts(tmp_path: Path, marker_first: bool) -> None:
    rows = _produce([([], False, False)], tmp_path)
    marker, full = {**rows[-2], "task_count": 1}, rows[-1]
    assert _fold([marker, full] if marker_first else [full, marker]).status == "invalid"


def test_duplicate_ids_are_not_silently_deduplicated(tmp_path: Path) -> None:
    tasks = [_task(id="a", title="A"), _task(id="a", title="A again")]
    rows = _produce([(tasks, False, False)], tmp_path)
    assert _fold(rows).status == "invalid"
