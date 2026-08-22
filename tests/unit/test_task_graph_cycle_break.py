"""A board that already carries a declared dependency cycle must still schedule.

Kahn's algorithm used to return ``[]`` for such a board, which left every task
in the cycle without a position in any ordering - the run idled until its
wall-clock timeout. The graph now opens the cycle at construction time by
dropping one declared edge, records which edge and which cycle, and warns.
"""

from __future__ import annotations

import logging

import pytest
from bernstein.core.dep_validator import DependencyValidator
from bernstein.core.models import Complexity, Scope, Task, TaskStatus

from bernstein.core.knowledge.task_graph import TaskGraph

LOGGER = "bernstein.core.knowledge.task_graph"


def _t(
    *,
    id: str,
    priority: int = 2,
    depends_on: list[str] | None = None,
    owned_files: list[str] | None = None,
    estimated_minutes: int = 30,
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description="desc",
        role="backend",
        priority=priority,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        depends_on=depends_on or [],
        owned_files=owned_files or [],
        estimated_minutes=estimated_minutes,
    )


def _cyclic_board() -> list[Task]:
    """A -> B -> C -> A, plus an unrelated D."""
    return [
        _t(id="A", depends_on=["C"]),
        _t(id="B", depends_on=["A"]),
        _t(id="C", depends_on=["B"]),
        _t(id="D"),
    ]


class TestPersistedCycleIsBrokenDeterministically:
    def test_every_task_still_gets_a_position(self) -> None:
        order = TaskGraph(_cyclic_board()).topological_order()
        assert set(order) == {"A", "B", "C", "D"}

    def test_unrelated_task_is_still_ordered(self) -> None:
        order = TaskGraph(_cyclic_board()).topological_order()
        assert "D" in order

    def test_same_board_gives_byte_identical_output(self) -> None:
        first = TaskGraph(_cyclic_board())
        second = TaskGraph(_cyclic_board())
        assert first.topological_order() == second.topological_order()
        assert [(b.edge.source, b.edge.target) for b in first.cycle_breaks] == [
            (b.edge.source, b.edge.target) for b in second.cycle_breaks
        ]

    def test_repeat_calls_on_one_graph_are_stable(self) -> None:
        g = TaskGraph(_cyclic_board())
        assert g.topological_order() == g.topological_order()

    def test_newest_declared_edge_in_the_cycle_is_the_one_dropped(self) -> None:
        """Edges are recorded in insertion order; the last one added closes the cycle."""
        g = TaskGraph(_cyclic_board())
        assert len(g.cycle_breaks) == 1
        broken = g.cycle_breaks[0]
        assert (broken.edge.source, broken.edge.target) == ("B", "C")
        assert set(broken.cycle) == {"A", "B", "C"}

    def test_dropped_edge_is_gone_from_the_graph(self) -> None:
        g = TaskGraph(_cyclic_board())
        assert ("B", "C") not in {(e.source, e.target) for e in g.edges}
        assert "C" not in g.dependents("B")
        assert "B" not in g.dependencies("C")

    def test_break_is_logged_with_the_edge_and_the_cycle(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            TaskGraph(_cyclic_board())
        assert "B" in caplog.text
        assert "C" in caplog.text
        assert "A" in caplog.text

    def test_critical_path_is_no_longer_empty(self) -> None:
        assert TaskGraph(_cyclic_board()).critical_path()

    def test_acyclic_board_records_no_break(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            g = TaskGraph(
                [
                    _t(id="A"),
                    _t(id="B", depends_on=["A"]),
                    _t(id="C", depends_on=["B"]),
                ]
            )
        assert g.cycle_breaks == []
        assert caplog.text == ""

    def test_task_depending_on_itself_is_opened(self) -> None:
        """A self-referential depends_on is a one-node cycle, not an infinite loop."""
        g = TaskGraph([_t(id="A", depends_on=["A"]), _t(id="B")])
        assert [(b.edge.source, b.edge.target) for b in g.cycle_breaks] == [("A", "A")]
        assert set(g.topological_order()) == {"A", "B"}

    def test_task_downstream_of_a_cycle_still_follows_it(self) -> None:
        g = TaskGraph(
            [
                _t(id="A", depends_on=["C"]),
                _t(id="B", depends_on=["A"]),
                _t(id="C", depends_on=["B"]),
                _t(id="X", depends_on=["A"]),
            ]
        )
        order = g.topological_order()
        assert set(order) == {"A", "B", "C", "X"}
        assert order.index("A") < order.index("X")

    def test_two_independent_cycles_are_both_opened(self) -> None:
        g = TaskGraph(
            [
                _t(id="A", depends_on=["B"]),
                _t(id="B", depends_on=["A"]),
                _t(id="C", depends_on=["D"]),
                _t(id="D", depends_on=["C"]),
            ]
        )
        assert len(g.cycle_breaks) == 2
        assert set(g.topological_order()) == {"A", "B", "C", "D"}


class TestCycleIsReportedNotSilentlyRepaired:
    def test_graph_names_the_tasks_in_the_cycle(self) -> None:
        g = TaskGraph(_cyclic_board())
        assert [set(b.cycle) for b in g.cycle_breaks] == [{"A", "B", "C"}]

    def test_dependency_validator_still_raises_on_a_declared_cycle(self) -> None:
        with pytest.raises(ValueError, match="cycle"):
            DependencyValidator().topological_order(_cyclic_board())

    def test_dependency_validator_still_reports_the_cycle(self) -> None:
        result = DependencyValidator().validate(_cyclic_board())
        assert result.cycles
        assert not result.valid

    def test_acyclic_board_still_orders_without_raising(self) -> None:
        tasks = [_t(id="A"), _t(id="B", depends_on=["A"])]
        assert DependencyValidator().topological_order(tasks) == ["A", "B"]


class TestBackstopOnlyOpensDeclaredEdges:
    def test_reviewer_overlap_board_needs_no_break_at_all(self) -> None:
        """The inferred edge is never created, so the backstop has nothing to open."""
        g = TaskGraph(
            [
                _t(id="9e1b8993743d", owned_files=["pr_gen.py"]),
                _t(id="338a6e0747ab", depends_on=["9e1b8993743d"], owned_files=["pr_gen.py"]),
            ]
        )
        assert g.cycle_breaks == []
        assert len(g.topological_order()) == 2

    def test_declared_cycle_over_shared_files_drops_a_declared_edge(self) -> None:
        g = TaskGraph(
            [
                _t(id="A", depends_on=["B"], owned_files=["shared.py"]),
                _t(id="B", depends_on=["A"], owned_files=["shared.py"]),
            ]
        )
        assert [b.edge.edge_type for b in g.cycle_breaks] == ["depends_on"]
