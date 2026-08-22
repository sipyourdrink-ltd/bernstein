"""Inferred file-overlap edges must not contradict explicit ``depends_on``.

A reviewer task typically owns every file its workers touch and depends on
all of them. At equal priority the file-overlap chain is ordered by
``(priority, id)``, so a worker whose id sorts after the reviewer's used to
receive an inferred edge running the opposite way to the explicit
dependency - a 2-cycle that emptied ``topological_order``.
"""

from __future__ import annotations

import logging

import pytest
from bernstein.core.models import Complexity, Scope, Task, TaskStatus

from bernstein.core.knowledge.task_graph import TaskGraph

# Ids taken verbatim from the run that surfaced this: at priority 2 the
# reviewer's id sorts before two of the three workers'.
REVIEWER = "338a6e0747ab"
WORKER_AFTER_A = "9e1b8993743d"
WORKER_AFTER_B = "fe132e87652c"
WORKER_BEFORE = "0c6c6f3004cb"

LOGGER = "bernstein.core.knowledge.task_graph"


def _t(
    *,
    id: str,
    role: str = "backend",
    priority: int = 2,
    depends_on: list[str] | None = None,
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description="desc",
        role=role,
        priority=priority,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        depends_on=depends_on or [],
        owned_files=owned_files or [],
        estimated_minutes=30,
    )


def _review_board() -> list[Task]:
    """Three workers plus a reviewer that owns every file and depends on all."""
    return [
        _t(id=WORKER_AFTER_A, owned_files=["pr_gen.py"]),
        _t(id=WORKER_AFTER_B, owned_files=["src/runner.py"]),
        _t(id=WORKER_BEFORE, owned_files=["src/store.py"]),
        _t(
            id=REVIEWER,
            role="reviewer",
            depends_on=[WORKER_AFTER_A, WORKER_AFTER_B, WORKER_BEFORE],
            owned_files=["pr_gen.py", "src/runner.py", "src/store.py"],
        ),
    ]


class TestOverlapEdgeNeverClosesCycle:
    def test_review_board_orders_every_task(self) -> None:
        g = TaskGraph(_review_board())
        order = g.topological_order()
        assert set(order) == {REVIEWER, WORKER_AFTER_A, WORKER_AFTER_B, WORKER_BEFORE}
        for worker in (WORKER_AFTER_A, WORKER_AFTER_B, WORKER_BEFORE):
            assert order.index(worker) < order.index(REVIEWER)

    def test_review_board_logs_no_cycle_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            g = TaskGraph(_review_board())
            assert g.topological_order()
        assert [r for r in caplog.records if r.name == LOGGER] == []
        assert g.cycle_breaks == []

    def test_explicit_edges_survive_and_no_reverse_overlap_edge(self) -> None:
        g = TaskGraph(_review_board())
        explicit = {(e.source, e.target) for e in g.edges if e.edge_type == "depends_on"}
        assert explicit == {
            (WORKER_AFTER_A, REVIEWER),
            (WORKER_AFTER_B, REVIEWER),
            (WORKER_BEFORE, REVIEWER),
        }
        assert all(e.source != REVIEWER for e in g.edges if e.edge_type == "file_overlap")

    def test_transitive_dependency_path_also_blocks_the_inferred_edge(self) -> None:
        """The contradiction can be indirect: a -> b -> c explicitly, c -> a inferred."""
        g = TaskGraph(
            [
                _t(id="c_last", priority=1, owned_files=["shared.py"], depends_on=["b_mid"]),
                _t(id="b_mid", priority=2, depends_on=["a_first"]),
                _t(id="a_first", priority=3, owned_files=["shared.py"]),
            ]
        )
        order = g.topological_order()
        assert set(order) == {"a_first", "b_mid", "c_last"}
        assert order.index("a_first") < order.index("b_mid") < order.index("c_last")

    def test_overlap_edge_still_added_when_it_closes_nothing(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", priority=1, owned_files=["src/app.py"]),
                _t(id="t2", priority=2, owned_files=["src/app.py"]),
            ]
        )
        assert [(e.source, e.target, e.edge_type) for e in g.edges] == [("t1", "t2", "file_overlap")]


class TestGenuineCycleStillDetected:
    """The fix must not repair a declared cycle into silence."""

    def test_mutual_depends_on_is_still_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            g = TaskGraph(
                [
                    _t(id="t1", depends_on=["t2"]),
                    _t(id="t2", depends_on=["t1"]),
                ]
            )
        assert [set(b.cycle) for b in g.cycle_breaks] == [{"t1", "t2"}]
        assert "cycle" in caplog.text.lower()
        assert "t1" in caplog.text and "t2" in caplog.text

    def test_three_task_depends_on_cycle_is_still_reported(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", depends_on=["t3"]),
                _t(id="t2", depends_on=["t1"]),
                _t(id="t3", depends_on=["t2"]),
            ]
        )
        assert [set(b.cycle) for b in g.cycle_breaks] == [{"t1", "t2", "t3"}]
        assert set(g.topological_order()) == {"t1", "t2", "t3"}
