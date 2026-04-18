"""Tests for the task dependency graph module."""

from __future__ import annotations

import pytest
from bernstein.core.models import Complexity, Scope, Task, TaskStatus

from bernstein.core.knowledge.task_graph import BLOCKING_EDGE_TYPES, EdgeType, GraphAnalysis, TaskGraph

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
    result_summary: str | None = None,
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
        result_summary=result_summary,
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

    def test_serialisation_includes_semantic_type(self) -> None:
        g = TaskGraph(
            [
                _t(id="t1"),
                _t(id="t2", depends_on=["t1"]),
            ]
        )
        d = g.to_dict()
        assert d["edges"][0]["semantic_type"] == "blocks"


# ---------------------------------------------------------------------------
# EdgeType enum
# ---------------------------------------------------------------------------


class TestEdgeType:
    def test_all_values(self) -> None:
        assert set(EdgeType) == {
            EdgeType.BLOCKS,
            EdgeType.INFORMS,
            EdgeType.VALIDATES,
            EdgeType.TRANSFORMS,
        }

    def test_blocking_set(self) -> None:
        assert {EdgeType.BLOCKS, EdgeType.VALIDATES} == BLOCKING_EDGE_TYPES

    def test_str_value(self) -> None:
        assert EdgeType.BLOCKS == "blocks"
        assert EdgeType.INFORMS == "informs"


# ---------------------------------------------------------------------------
# add_dependency (public API for typed edges)
# ---------------------------------------------------------------------------


class TestAddDependency:
    def test_add_blocks_edge(self) -> None:
        g = TaskGraph([_t(id="t1"), _t(id="t2")])
        g.add_dependency("t1", "t2", EdgeType.BLOCKS)
        assert len(g.edges) == 1
        assert g.edges[0].semantic_type == EdgeType.BLOCKS

    def test_add_informs_edge(self) -> None:
        g = TaskGraph([_t(id="t1"), _t(id="t2")])
        g.add_dependency("t1", "t2", EdgeType.INFORMS)
        assert g.edges[0].semantic_type == EdgeType.INFORMS

    def test_add_validates_edge(self) -> None:
        g = TaskGraph([_t(id="t1"), _t(id="t2")])
        g.add_dependency("t1", "t2", EdgeType.VALIDATES)
        assert g.edges[0].semantic_type == EdgeType.VALIDATES

    def test_add_transforms_edge(self) -> None:
        g = TaskGraph([_t(id="t1"), _t(id="t2")])
        g.add_dependency("t1", "t2", EdgeType.TRANSFORMS)
        assert g.edges[0].semantic_type == EdgeType.TRANSFORMS

    def test_missing_source_raises(self) -> None:
        g = TaskGraph([_t(id="t1")])
        with pytest.raises(KeyError, match="missing"):
            g.add_dependency("missing", "t1", EdgeType.BLOCKS)

    def test_missing_target_raises(self) -> None:
        g = TaskGraph([_t(id="t1")])
        with pytest.raises(KeyError, match="missing"):
            g.add_dependency("t1", "missing", EdgeType.BLOCKS)

    def test_default_edge_type_is_blocks(self) -> None:
        g = TaskGraph([_t(id="t1"), _t(id="t2")])
        g.add_dependency("t1", "t2")
        assert g.edges[0].semantic_type == EdgeType.BLOCKS


# ---------------------------------------------------------------------------
# Typed edge scheduling (ready_tasks with edge types)
# ---------------------------------------------------------------------------


class TestTypedEdgeScheduling:
    def test_blocks_edge_prevents_scheduling(self) -> None:
        """BLOCKS predecessor not done → successor not ready."""
        g = TaskGraph([_t(id="t1", status="open"), _t(id="t2", status="open")])
        g.add_dependency("t1", "t2", EdgeType.BLOCKS)
        assert g.ready_tasks() == ["t1"]

    def test_blocks_edge_done_allows_scheduling(self) -> None:
        g = TaskGraph([_t(id="t1", status="done"), _t(id="t2", status="open")])
        g.add_dependency("t1", "t2", EdgeType.BLOCKS)
        assert g.ready_tasks() == ["t2"]

    def test_informs_edge_does_not_block(self) -> None:
        """INFORMS predecessor not done → successor IS ready."""
        g = TaskGraph([_t(id="t1", status="open"), _t(id="t2", status="open")])
        g.add_dependency("t1", "t2", EdgeType.INFORMS)
        ready = g.ready_tasks()
        assert "t1" in ready
        assert "t2" in ready

    def test_transforms_edge_does_not_block(self) -> None:
        """TRANSFORMS predecessor not done → successor IS ready."""
        g = TaskGraph([_t(id="t1", status="open"), _t(id="t2", status="open")])
        g.add_dependency("t1", "t2", EdgeType.TRANSFORMS)
        ready = g.ready_tasks()
        assert "t1" in ready
        assert "t2" in ready

    def test_validates_edge_blocks_scheduling(self) -> None:
        """VALIDATES predecessor not done → validator not ready."""
        g = TaskGraph([_t(id="t1", status="open"), _t(id="validator", status="open")])
        g.add_dependency("t1", "validator", EdgeType.VALIDATES)
        assert g.ready_tasks() == ["t1"]

    def test_validates_edge_done_allows_scheduling(self) -> None:
        g = TaskGraph([_t(id="t1", status="done"), _t(id="validator", status="open")])
        g.add_dependency("t1", "validator", EdgeType.VALIDATES)
        assert g.ready_tasks() == ["validator"]

    def test_mixed_edges_only_blocking_matters(self) -> None:
        """Task with both INFORMS and BLOCKS deps: only BLOCKS blocks."""
        g = TaskGraph(
            [
                _t(id="info", status="open"),
                _t(id="blocker", status="done"),
                _t(id="worker", status="open"),
            ]
        )
        g.add_dependency("info", "worker", EdgeType.INFORMS)
        g.add_dependency("blocker", "worker", EdgeType.BLOCKS)
        ready = g.ready_tasks()
        assert "worker" in ready

    def test_mixed_edges_blocks_not_done(self) -> None:
        """If the BLOCKS dep isn't done, task is not ready even with INFORMS."""
        g = TaskGraph(
            [
                _t(id="info", status="done"),
                _t(id="blocker", status="open"),
                _t(id="worker", status="open"),
            ]
        )
        g.add_dependency("info", "worker", EdgeType.INFORMS)
        g.add_dependency("blocker", "worker", EdgeType.BLOCKS)
        ready = g.ready_tasks()
        assert "worker" not in ready

    def test_existing_depends_on_defaults_to_blocks(self) -> None:
        """Edges from Task.depends_on default to BLOCKS semantic type."""
        g = TaskGraph(
            [
                _t(id="t1", status="open"),
                _t(id="t2", depends_on=["t1"], status="open"),
            ]
        )
        edges_to_t2 = g.edges_to("t2")
        assert len(edges_to_t2) == 1
        assert edges_to_t2[0].semantic_type == EdgeType.BLOCKS
        assert g.ready_tasks() == ["t1"]


# ---------------------------------------------------------------------------
# Validation failure handling
# ---------------------------------------------------------------------------


class TestValidationFailure:
    def test_validator_failure_returns_predecessor(self) -> None:
        g = TaskGraph([_t(id="impl"), _t(id="validator")])
        g.add_dependency("impl", "validator", EdgeType.VALIDATES)
        retries = g.tasks_to_retry_on_validation_failure("validator")
        assert retries == ["impl"]

    def test_no_validates_edges_returns_empty(self) -> None:
        g = TaskGraph([_t(id="t1"), _t(id="t2")])
        g.add_dependency("t1", "t2", EdgeType.BLOCKS)
        assert g.tasks_to_retry_on_validation_failure("t2") == []

    def test_multiple_validated_predecessors(self) -> None:
        g = TaskGraph([_t(id="a"), _t(id="b"), _t(id="validator")])
        g.add_dependency("a", "validator", EdgeType.VALIDATES)
        g.add_dependency("b", "validator", EdgeType.VALIDATES)
        retries = g.tasks_to_retry_on_validation_failure("validator")
        assert set(retries) == {"a", "b"}

    def test_validated_by_returns_validators(self) -> None:
        g = TaskGraph([_t(id="impl"), _t(id="v1"), _t(id="v2")])
        g.add_dependency("impl", "v1", EdgeType.VALIDATES)
        g.add_dependency("impl", "v2", EdgeType.VALIDATES)
        assert set(g.validated_by("impl")) == {"v1", "v2"}


# ---------------------------------------------------------------------------
# Edge query helpers
# ---------------------------------------------------------------------------


class TestEdgeQueries:
    def test_edges_to(self) -> None:
        g = TaskGraph([_t(id="a"), _t(id="b"), _t(id="c")])
        g.add_dependency("a", "c", EdgeType.BLOCKS)
        g.add_dependency("b", "c", EdgeType.INFORMS)
        edges = g.edges_to("c")
        assert len(edges) == 2

    def test_edges_to_by_type(self) -> None:
        g = TaskGraph([_t(id="a"), _t(id="b"), _t(id="c")])
        g.add_dependency("a", "c", EdgeType.BLOCKS)
        g.add_dependency("b", "c", EdgeType.INFORMS)
        assert len(g.edges_to_by_type("c", EdgeType.BLOCKS)) == 1
        assert len(g.edges_to_by_type("c", EdgeType.INFORMS)) == 1
        assert len(g.edges_to_by_type("c", EdgeType.VALIDATES)) == 0


# ---------------------------------------------------------------------------
# Predecessor context (INFORMS / TRANSFORMS)
# ---------------------------------------------------------------------------


class TestPredecessorContext:
    def test_informs_predecessor_context(self) -> None:
        g = TaskGraph(
            [
                _t(id="research", status="done", result_summary="Found 3 APIs"),
                _t(id="impl", status="open"),
            ]
        )
        g.add_dependency("research", "impl", EdgeType.INFORMS)
        ctx = g.predecessor_context("impl")
        assert len(ctx) == 1
        assert ctx[0]["task_id"] == "research"
        assert ctx[0]["result_summary"] == "Found 3 APIs"
        assert ctx[0]["edge_type"] == "informs"

    def test_transforms_predecessor_context(self) -> None:
        g = TaskGraph(
            [
                _t(id="parser", status="done", result_summary="Parsed config"),
                _t(id="consumer", status="open"),
            ]
        )
        g.add_dependency("parser", "consumer", EdgeType.TRANSFORMS)
        ctx = g.predecessor_context("consumer")
        assert len(ctx) == 1
        assert ctx[0]["edge_type"] == "transforms"

    def test_blocks_not_in_predecessor_context(self) -> None:
        """BLOCKS edges do not contribute to predecessor context."""
        g = TaskGraph(
            [
                _t(id="dep", status="done", result_summary="Done"),
                _t(id="task", status="open"),
            ]
        )
        g.add_dependency("dep", "task", EdgeType.BLOCKS)
        assert g.predecessor_context("task") == []

    def test_incomplete_predecessor_excluded(self) -> None:
        """Only DONE predecessors appear in context."""
        g = TaskGraph(
            [
                _t(id="research", status="open", result_summary="Partial"),
                _t(id="impl", status="open"),
            ]
        )
        g.add_dependency("research", "impl", EdgeType.INFORMS)
        assert g.predecessor_context("impl") == []

    def test_empty_summary_excluded(self) -> None:
        g = TaskGraph(
            [
                _t(id="research", status="done"),
                _t(id="impl", status="open"),
            ]
        )
        g.add_dependency("research", "impl", EdgeType.INFORMS)
        ctx = g.predecessor_context("impl")
        assert len(ctx) == 1
        assert ctx[0]["result_summary"] == ""
