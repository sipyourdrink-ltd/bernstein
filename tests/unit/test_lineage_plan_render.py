"""`bernstein lineage plan`: a run's whole task graph, read back from its journal.

The decomposition was already recorded — the orchestrator appends a
`plan.graph.full` row carrying the goal and every task node — but nothing read
it back as a plan. Every review surface ships per-task, so an operator
approving task 7 of 20 had no rendering of what the other 19 add up to (#4958).

The load-bearing property is byte-identity: a plan artefact that renders
differently on two reads of one journal cannot be diffed, hashed, or cited.
It holds because `project_board` never reads a row's wall-clock envelope and
because ordering here is a journal fact — nodes sorted by task id, each node's
dependencies sorted within it — rather than a render-time choice.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.lineage_cmd import lineage_cmd
from bernstein.core.lineage.plan_render import plan_from_journal, read_run_plan, render_plan_text

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "run-4958"

#: A three-task graph with a real dependency chain and one independent task.
_NODES = [
    {"id": "T-3", "role": "reviewer", "title": "Review the API change", "depends_on": ["T-1", "T-2"]},
    {"id": "T-1", "role": "backend", "title": "Add the endpoint", "depends_on": []},
    {"id": "T-2", "role": "tests", "title": "Cover the endpoint", "depends_on": ["T-1"]},
]

_GOAL = "Add a /health endpoint with tests and a review"


def _journal_rows() -> list[dict[str, Any]]:
    return [
        {"event": "session_start", "ts": 1.0},
        {"event": "plan.graph.full", "ts": 2.0, "goal": _GOAL, "nodes": _NODES},
        {"event": "task_claimed", "ts": 3.0, "task_id": "T-1"},
    ]


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    """A runs directory holding one journal with a recorded plan."""
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text(
        "\n".join(json.dumps(row) for row in _journal_rows()) + "\n", encoding="utf-8"
    )
    return tmp_path / "runs"


def _plan(runs_dir: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(lineage_cmd, ["plan", RUN_ID, "--runs-dir", str(runs_dir), *args])
    return result.exit_code, result.output


# ---------------------------------------------------------------------------
# The acceptance criteria, as the issue states them
# ---------------------------------------------------------------------------


def test_the_command_exits_successfully(runs_dir: Path) -> None:
    code, _ = _plan(runs_dir)
    assert code == 0


def test_the_output_contains_the_goal(runs_dir: Path) -> None:
    _, output = _plan(runs_dir)
    assert _GOAL in output


def test_every_task_appears(runs_dir: Path) -> None:
    _, output = _plan(runs_dir)
    for node in _NODES:
        assert str(node["id"]) in output, f"{node['id']} missing from the rendering"


def test_every_declared_intent_appears(runs_dir: Path) -> None:
    """A task's declared intent is its role and title, as the journal records them."""
    _, output = _plan(runs_dir)
    for node in _NODES:
        assert str(node["role"]) in output
        assert str(node["title"]) in output


def test_every_dependency_appears(runs_dir: Path) -> None:
    """The edges, not just the nodes — a graph without them is a list."""
    _, output = _plan(runs_dir)
    review_line = next(line for line in output.splitlines() if "depends on: T-1, T-2" in line)
    assert review_line
    assert "depends on: T-1" in output


def test_rendering_the_same_journal_twice_produces_identical_bytes(runs_dir: Path) -> None:
    """The property that makes the output an artefact rather than a report."""
    first_code, first = _plan(runs_dir)
    second_code, second = _plan(runs_dir)
    assert (first_code, second_code) == (0, 0)
    assert first == second


def test_json_output_is_stable_too(runs_dir: Path) -> None:
    """The machine-readable shape carries the same guarantee."""
    _, first = _plan(runs_dir, "--json")
    _, second = _plan(runs_dir, "--json")
    assert first == second
    payload = json.loads(first)
    assert payload["goal"] == _GOAL
    assert [node["id"] for node in payload["nodes"]] == ["T-1", "T-2", "T-3"]


# ---------------------------------------------------------------------------
# What the rendering must not claim
# ---------------------------------------------------------------------------


def test_a_journal_with_no_plan_row_is_not_a_run_that_planned_nothing(tmp_path: Path) -> None:
    """The distinction a renderer that printed an empty graph would erase."""
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text(json.dumps({"event": "session_start", "ts": 1.0}) + "\n", encoding="utf-8")
    code, output = _plan(tmp_path / "runs")
    assert code == 0
    assert "never recorded" in output
    assert "not the same as a run that planned nothing" in output


def test_a_missing_journal_reports_that_rather_than_raising(tmp_path: Path) -> None:
    """ "Nothing recorded it" is an answer to "what did this run plan"."""
    plan = read_run_plan(tmp_path / "absent" / "journal.jsonl")
    assert plan.recorded is False
    assert plan.nodes == ()


def test_a_run_id_that_escapes_the_runs_root_is_refused(tmp_path: Path) -> None:
    """RUN_ID arrives from the command line, so the path is derived, not joined.

    `tests/unit/test_path_containment.py` enforces this mechanically for every
    journal reader; the case is pinned here too so the refusal is visible where
    the command is read.
    """
    outside = tmp_path / "outside" / "journal.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_text(
        json.dumps({"event": "plan.graph.full", "goal": "leaked", "nodes": []}) + "\n",
        encoding="utf-8",
    )
    runs = tmp_path / "runs"
    runs.mkdir()
    result = CliRunner().invoke(lineage_cmd, ["plan", "../outside", "--runs-dir", str(runs)])
    assert result.exit_code == 1
    assert "does not resolve inside" in result.output
    assert "leaked" not in result.output


def test_nodes_are_ordered_by_task_id_not_journal_order() -> None:
    """Write order is an orchestration artefact; the rendering must not inherit it."""
    plan = plan_from_journal(_journal_rows())
    assert [node.task_id for node in plan.nodes] == ["T-1", "T-2", "T-3"]


def test_a_dependencyless_task_is_reported_as_a_root() -> None:
    """Where an operator starts reading the graph."""
    plan = plan_from_journal(_journal_rows())
    assert [node.task_id for node in plan.roots] == ["T-1"]


def test_the_rendering_carries_no_clock(tmp_path: Path) -> None:
    """Two journals differing only in timestamps render identically.

    This is what `project_board` not reading the wall-clock envelope buys, and
    it is the reason the byte-identity property survives a re-run rather than
    holding only within one process.
    """
    shifted = [dict(row, ts=float(row["ts"]) + 10_000) for row in _journal_rows()]
    assert render_plan_text(plan_from_journal(_journal_rows()), run_id=RUN_ID) == render_plan_text(
        plan_from_journal(shifted), run_id=RUN_ID
    )
