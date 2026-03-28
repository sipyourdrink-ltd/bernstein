"""Tests for the task dependency graph module."""

from __future__ import annotations

from bernstein.core.graph import GraphAnalysis, TaskGraph
from bernstein.core.models import Complexity, Scope, Task, TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _t(
    *,
    id: str,
    role: str = "backend",
    priority: int = 2,
    status: str = "open",
    depends_on: list[str] | None = None,
    owned_files: list[str] | None = None,
    estimated_minutes: int = 30,
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description="desc",
        role=role,
        priority=priority,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus(status),
        depends_on=depends_on or [],
        owned_files=owned_files or [],
        estimated_minutes=estimated_minutes,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestGraphConstruction:
    def test_empty_graph(self) -> None:
        g = TaskGraph([])
        assert g.nodes == []
        assert g.edges == []

    def test_single_task_no_deps(self) -> None:
        g = TaskGraph([_t(id="t1")])
        assert g.nodes == ["t1"]
        assert g.edges == []

    def test_explicit_dependency_creates_edge(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1"),
                _t(id="t2", depends_on=["t1"]),
            ]
        )
        assert len(g.edges) == 1
        assert g.edges[0].source == "t1"
        assert g.edges[0].target == "t2"
        assert g.edges[0].edge_type == "depends_on"

    def test_dependency_on_missing_task_ignored(self) -> None:
        g = TaskGraph([_t(id="t1", depends_on=["missing"])])
        assert g.edges == []

    def test_file_overlap_creates_edge(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", priority=1, owned_files=["src/app.py"]),
                _t(id="t2", priority=2, owned_files=["src/app.py"]),
            ]
        )
        assert len(g.edges) == 1
        assert g.edges[0].source == "t1"  # higher priority first
        assert g.edges[0].target == "t2"
        assert g.edges[0].edge_type == "file_overlap"

    def test_file_overlap_not_duplicated_with_explicit_dep(self) -> None:
        """If explicit dep already exists, file overlap doesn't add another."""
        g = TaskGraph(
            [
                _t(id="t1", priority=1, owned_files=["f.py"]),
                _t(id="t2", priority=2, depends_on=["t1"], owned_files=["f.py"]),
            ]
        )
        # One explicit dep edge, file overlap skipped because already connected
        assert len(g.edges) == 1
        assert g.edges[0].edge_type == "depends_on"


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    def test_linear_chain(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1"),
                _t(id="t2", depends_on=["t1"]),
                _t(id="t3", depends_on=["t2"]),
            ]
        )
        order = g.topological_order()
        assert order.index("t1") < order.index("t2") < order.index("t3")

    def test_diamond_dag(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1"),
                _t(id="t2", depends_on=["t1"]),
                _t(id="t3", depends_on=["t1"]),
                _t(id="t4", depends_on=["t2", "t3"]),
            ]
        )
        order = g.topological_order()
        assert order[0] == "t1"
        assert order[-1] == "t4"
        assert len(order) == 4

    def test_independent_tasks(self) -> None:
        g = TaskGraph([_t(id="t1"), _t(id="t2"), _t(id="t3")])
        order = g.topological_order()
        assert set(order) == {"t1", "t2", "t3"}


# ---------------------------------------------------------------------------
# Critical path
# ---------------------------------------------------------------------------


class TestCriticalPath:
    def test_single_task(self) -> None:
        g = TaskGraph([_t(id="t1", estimated_minutes=10)])
        assert g.critical_path() == ["t1"]
        assert g.critical_path_minutes() == 10

    def test_linear_chain(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", estimated_minutes=10),
                _t(id="t2", depends_on=["t1"], estimated_minutes=20),
                _t(id="t3", depends_on=["t2"], estimated_minutes=5),
            ]
        )
        assert g.critical_path() == ["t1", "t2", "t3"]
        assert g.critical_path_minutes() == 35

    def test_diamond_picks_longer_path(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", estimated_minutes=10),
                _t(id="t2", depends_on=["t1"], estimated_minutes=5),
                _t(id="t3", depends_on=["t1"], estimated_minutes=30),
                _t(id="t4", depends_on=["t2", "t3"], estimated_minutes=10),
            ]
        )
        cp = g.critical_path()
        assert cp == ["t1", "t3", "t4"]
        assert g.critical_path_minutes() == 50

    def test_empty_graph(self) -> None:
        g = TaskGraph([])
        assert g.critical_path() == []
        assert g.critical_path_minutes() == 0

    def test_independent_tasks_picks_longest(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", estimated_minutes=5),
                _t(id="t2", estimated_minutes=60),
                _t(id="t3", estimated_minutes=10),
            ]
        )
        assert g.critical_path() == ["t2"]


# ---------------------------------------------------------------------------
# Parallel width
# ---------------------------------------------------------------------------


class TestParallelWidth:
    def test_all_independent(self) -> None:
        g = TaskGraph([_t(id="t1"), _t(id="t2"), _t(id="t3")])
        assert g.parallel_width() == 3

    def test_linear_chain(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1"),
                _t(id="t2", depends_on=["t1"]),
                _t(id="t3", depends_on=["t2"]),
            ]
        )
        assert g.parallel_width() == 1

    def test_diamond(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1"),
                _t(id="t2", depends_on=["t1"]),
                _t(id="t3", depends_on=["t1"]),
                _t(id="t4", depends_on=["t2", "t3"]),
            ]
        )
        assert g.parallel_width() == 2  # t2 and t3 at same level

    def test_empty(self) -> None:
        g = TaskGraph([])
        assert g.parallel_width() == 0

    def test_wide_fan_out(self) -> None:
        root = _t(id="root")
        children = [_t(id=f"c{i}", depends_on=["root"]) for i in range(5)]
        g = TaskGraph([root, *children])
        assert g.parallel_width() == 5


# ---------------------------------------------------------------------------
# Bottleneck detection
# ---------------------------------------------------------------------------


class TestBottlenecks:
    def test_no_bottleneck_when_all_done(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", status="done"),
                _t(id="t2", depends_on=["t1"], status="done"),
            ]
        )
        assert g.bottlenecks() == []

    def test_single_bottleneck(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", status="in_progress"),
                _t(id="t2", depends_on=["t1"]),
                _t(id="t3", depends_on=["t1"]),
            ]
        )
        assert g.bottlenecks(threshold=2) == ["t1"]

    def test_threshold_filters(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", status="open"),
                _t(id="t2", depends_on=["t1"]),
            ]
        )
        assert g.bottlenecks(threshold=1) == ["t1"]
        assert g.bottlenecks(threshold=2) == []


# ---------------------------------------------------------------------------
# Ready tasks
# ---------------------------------------------------------------------------


class TestReadyTasks:
    def test_no_deps_are_ready(self) -> None:
        g = TaskGraph([_t(id="t1"), _t(id="t2")])
        assert set(g.ready_tasks()) == {"t1", "t2"}

    def test_unmet_dep_not_ready(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", status="open"),
                _t(id="t2", depends_on=["t1"], status="open"),
            ]
        )
        assert g.ready_tasks() == ["t1"]

    def test_met_dep_is_ready(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", status="done"),
                _t(id="t2", depends_on=["t1"], status="open"),
            ]
        )
        assert g.ready_tasks() == ["t2"]

    def test_done_tasks_not_listed(self) -> None:
        g = TaskGraph([_t(id="t1", status="done")])
        assert g.ready_tasks() == []


# ---------------------------------------------------------------------------
# Analysis & serialisation
# ---------------------------------------------------------------------------


class TestAnalysis:
    def test_analyse_returns_graph_analysis(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1", estimated_minutes=10),
                _t(id="t2", depends_on=["t1"], estimated_minutes=20),
            ]
        )
        a = g.analyse()
        assert isinstance(a, GraphAnalysis)
        assert a.critical_path == ["t1", "t2"]
        assert a.critical_path_minutes == 30
        assert a.parallel_width == 1


class TestSerialisation:
    def test_to_dict_structure(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1"),
                _t(id="t2", depends_on=["t1"]),
            ]
        )
        d = g.to_dict()
        assert "nodes" in d
        assert "edges" in d
        assert "critical_path" in d
        assert "parallel_width" in d
        assert "bottlenecks" in d
        assert len(d["nodes"]) == 2
        assert len(d["edges"]) == 1

    def test_save_creates_file(self, tmp_path: object) -> None:
        from pathlib import Path

        p = Path(str(tmp_path))
        g = TaskGraph([_t(id="t1")])
        g.save(p)
        assert (p / "task_graph.json").exists()

    def test_dependents_and_dependencies(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1"),
                _t(id="t2", depends_on=["t1"]),
                _t(id="t3", depends_on=["t1"]),
            ]
        )
        assert set(g.dependents("t1")) == {"t2", "t3"}
        assert g.dependencies("t2") == ["t1"]
        assert g.dependencies("t1") == []
