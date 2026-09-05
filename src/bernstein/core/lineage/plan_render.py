"""Render a run's whole task graph from its journal.

A run's decomposition is already in the journal. The orchestrator appends a
``plan.graph.full`` row whenever the executed graph changes, carrying the run's
goal and every task node with its role, title and dependencies
(:meth:`~bernstein.core.orchestration.orchestrator.Orchestrator._record_plan_graph_full`).
Nothing read it back as a plan, so every review surface was per-task: an
operator approving task 7 of 20 had no rendering of what the other 19 add up
to (#4958).

This module is the rendering and nothing else. It reads through
:func:`~bernstein.core.replay.review_board.project_board`, which already folds
those rows canonically, rather than opening a second path to the same data.

Determinism is the contract. ``project_board`` never reads the wall-clock
envelope on a row, and the ordering here is a journal fact rather than a
render-time choice: nodes come back sorted by task id, and a task's
dependencies are sorted within the node. Re-rendering the same journal
therefore produces identical bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.journal import load_events
from bernstein.core.replay.review_board import project_board

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = [
    "PlanNode",
    "RunPlan",
    "plan_from_journal",
    "read_run_plan",
    "render_plan_text",
]


@dataclass(frozen=True, slots=True)
class PlanNode:
    """One task in the plan, as the journal recorded it."""

    task_id: str
    role: str
    title: str
    depends_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunPlan:
    """A run's goal and the whole task graph it was decomposed into.

    ``recorded`` distinguishes "this run wrote no plan row" from "this run
    planned nothing". A journal with no ``plan.graph.full`` row is not a run
    with an empty plan: the graph may simply never have been recorded, and a
    renderer that prints an empty graph for both says something untrue about
    one of them.
    """

    goal: str
    nodes: tuple[PlanNode, ...]
    recorded: bool

    @property
    def roots(self) -> tuple[PlanNode, ...]:
        """Nodes nothing in this plan depends on being finished first."""
        return tuple(node for node in self.nodes if not node.depends_on)


def plan_from_journal(events: Sequence[Mapping[str, Any]]) -> RunPlan:
    """Project journal *events* into the plan they recorded.

    Args:
        events: Journal rows in append order, as
            :func:`bernstein.core.replay.journal.load_events` returns them.

    Returns:
        The plan. ``recorded`` is False when no ``plan.graph.full`` row was
        folded, in which case ``nodes`` is empty and ``goal`` is "".
    """
    board = project_board(events)
    run = board.get("run", {})
    raw_nodes = run.get("plan_nodes")
    if not isinstance(raw_nodes, list):
        return RunPlan(goal=str(run.get("goal", "") or ""), nodes=(), recorded=False)
    nodes = tuple(
        PlanNode(
            task_id=str(node.get("id", "")),
            role=str(node.get("role", "")),
            title=str(node.get("title", "")),
            depends_on=tuple(str(dep) for dep in node.get("depends_on", ())),
        )
        for node in raw_nodes
    )
    return RunPlan(goal=str(run.get("goal", "") or ""), nodes=nodes, recorded=True)


def read_run_plan(journal_path: Path) -> RunPlan:
    """Read *journal_path* and project the plan it recorded.

    A missing journal is reported as an unrecorded plan rather than raised:
    the caller's question is "what did this run plan", and "nothing recorded
    it" is an answer to that.
    """
    if not journal_path.exists():
        return RunPlan(goal="", nodes=(), recorded=False)
    loaded = load_events(journal_path)
    return plan_from_journal(loaded.events)


def render_plan_text(plan: RunPlan, *, run_id: str) -> str:
    """Render *plan* as the artefact an operator reads.

    Every line is derived from the plan alone - no timestamps, no counts taken
    at render time - so two renderings of one journal are byte-identical.
    """
    lines = [f"run: {run_id}"]
    lines.append(f"goal: {plan.goal}" if plan.goal else "goal: (not recorded)")

    if not plan.recorded:
        lines.append("")
        lines.append("No plan.graph.full row in this journal: the run's task graph was never recorded.")
        lines.append("This is not the same as a run that planned nothing.")
        return "\n".join(lines) + "\n"

    lines.append(f"tasks: {len(plan.nodes)}")
    lines.append("")
    for node in plan.nodes:
        title = node.title or "(untitled)"
        lines.append(f"  {node.task_id}  [{node.role or 'unassigned'}]  {title}")
        if node.depends_on:
            lines.append(f"      depends on: {', '.join(node.depends_on)}")
        else:
            lines.append("      depends on: nothing")
    return "\n".join(lines) + "\n"
