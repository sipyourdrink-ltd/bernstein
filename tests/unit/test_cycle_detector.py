"""Tests for dependency cycle detection (TASK-006)."""

from __future__ import annotations

from bernstein.core.cycle_detector import detect_cycles, validate_plan_acyclic
from bernstein.core.models import Complexity, Scope, Task, TaskStatus


def _t(id: str, depends_on: list[str] | None = None) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description="desc",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        depends_on=depends_on or [],
    )


class TestDetectCycles:
    def test_no_tasks(self) -> None:
        report = detect_cycles([])
        assert not report.has_cycles
        assert report.cycles == []

    def test_single_task_no_deps(self) -> None:
        report = detect_cycles([_t("t1")])
        assert not report.has_cycles

    def test_linear_chain_no_cycle(self) -> None:
        tasks = [_t("t1"), _t("t2", ["t1"]), _t("t3", ["t2"])]
        report = detect_cycles(tasks)
        assert not report.has_cycles

    def test_simple_two_node_cycle(self) -> None:
        tasks = [_t("t1", ["t2"]), _t("t2", ["t1"])]
        report = detect_cycles(tasks)
        assert report.has_cycles
        assert len(report.cycles) >= 1
        # Both nodes must appear in the cycle
        cycle_nodes = set(report.cycles[0])
        assert "t1" in cycle_nodes
        assert "t2" in cycle_nodes

    def test_three_node_cycle(self) -> None:
        tasks = [_t("t1", ["t3"]), _t("t2", ["t1"]), _t("t3", ["t2"])]
        report = detect_cycles(tasks)
        assert report.has_cycles
        cycle_nodes = set(report.cycles[0])
        assert cycle_nodes == {"t1", "t2", "t3"}

    def test_cycle_with_non_cyclic_branch(self) -> None:
        tasks = [
            _t("t1"),
            _t("t2", ["t1"]),
            _t("t3", ["t2"]),
            _t("t4", ["t3", "t5"]),
            _t("t5", ["t4"]),  # cycle between t4 and t5
        ]
        report = detect_cycles(tasks)
        assert report.has_cycles
        # The cycle should involve t4 and t5
        all_cycle_nodes: set[str] = set()
        for cycle in report.cycles:
            all_cycle_nodes.update(cycle)
        assert "t4" in all_cycle_nodes
        assert "t5" in all_cycle_nodes

    def test_dangling_dependency_ignored(self) -> None:
        tasks = [_t("t1", ["missing"]), _t("t2", ["t1"])]
        report = detect_cycles(tasks)
        assert not report.has_cycles

    def test_self_loop(self) -> None:
        tasks = [_t("t1", ["t1"])]
        report = detect_cycles(tasks)
        assert report.has_cycles
        assert len(report.cycles) >= 1

    def test_summary_formatting(self) -> None:
        tasks = [_t("t1", ["t2"]), _t("t2", ["t1"])]
        report = detect_cycles(tasks)
        assert "cycle" in report.summary.lower()
        assert "->" in report.summary

    def test_diamond_no_cycle(self) -> None:
        tasks = [
            _t("t1"),
            _t("t2", ["t1"]),
            _t("t3", ["t1"]),
            _t("t4", ["t2", "t3"]),
        ]
        report = detect_cycles(tasks)
        assert not report.has_cycles

    def test_disconnected_graphs(self) -> None:
        tasks = [
            _t("a1"),
            _t("a2", ["a1"]),
            _t("b1", ["b2"]),
            _t("b2", ["b1"]),
        ]
        report = detect_cycles(tasks)
        assert report.has_cycles
        all_cycle_nodes: set[str] = set()
        for cycle in report.cycles:
            all_cycle_nodes.update(cycle)
        assert "b1" in all_cycle_nodes
        assert "b2" in all_cycle_nodes
        # a1, a2 should not be in any cycle
        assert "a1" not in all_cycle_nodes


class TestValidatePlanAcyclic:
    def test_acyclic_plan(self) -> None:
        tasks = [_t("t1"), _t("t2", ["t1"])]
        report = validate_plan_acyclic(tasks)
        assert not report.has_cycles

    def test_cyclic_plan(self) -> None:
        tasks = [_t("t1", ["t2"]), _t("t2", ["t1"])]
        report = validate_plan_acyclic(tasks)
        assert report.has_cycles
        assert len(report.cycles) >= 1
