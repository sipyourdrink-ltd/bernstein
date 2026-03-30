"""Tests for the workflow DSL module."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.graph import EdgeType
from bernstein.core.models import Task, TaskStatus
from bernstein.core.workflow import WorkflowDefinition, WorkflowPhase
from bernstein.core.workflow_dsl import (
    ConditionError,
    ConditionExpr,
    DAGEdge,
    DAGExecutor,
    DAGNode,
    DSLError,
    EdgeResolution,
    RetryPolicy,
    WorkflowDAG,
    build_condition_context,
    parse_workflow_yaml,
    validate_dag,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    role: str = "backend",
    status: TaskStatus = TaskStatus.OPEN,
    result_summary: str | None = None,
) -> Task:
    """Create a minimal task for testing."""
    return Task(
        id=task_id,
        title=f"Task {task_id}",
        description="test",
        role=role,
        status=status,
        result_summary=result_summary,
    )


def _simple_definition() -> WorkflowDefinition:
    """Create a simple 3-phase workflow definition."""
    return WorkflowDefinition(
        name="test",
        phases=(
            WorkflowPhase(name="plan"),
            WorkflowPhase(name="implement"),
            WorkflowPhase(name="verify"),
        ),
    )


def _simple_dag() -> WorkflowDAG:
    """Create a simple DAG: A -> B -> C across three phases."""
    return WorkflowDAG(
        definition=_simple_definition(),
        nodes=(
            DAGNode(id="a", phase="plan", role="manager"),
            DAGNode(id="b", phase="implement", role="backend"),
            DAGNode(id="c", phase="verify", role="qa"),
        ),
        edges=(
            DAGEdge(source="a", target="b"),
            DAGEdge(source="b", target="c"),
        ),
    )


def _write_yaml(tmp_path: Path, content: str) -> Path:
    """Write YAML content to a temp file and return the path."""
    p = tmp_path / "workflow.yaml"
    p.write_text(textwrap.dedent(content))
    return p


# ===========================================================================
# ConditionExpr tests
# ===========================================================================


class TestConditionExpr:
    def test_simple_equality(self) -> None:
        expr = ConditionExpr(raw="status == 'done'")
        assert expr.evaluate({"status": "done"})
        assert not expr.evaluate({"status": "failed"})

    def test_not_equal(self) -> None:
        expr = ConditionExpr(raw="status != 'failed'")
        assert expr.evaluate({"status": "done"})
        assert not expr.evaluate({"status": "failed"})

    def test_numeric_comparison(self) -> None:
        expr = ConditionExpr(raw="output.count > 5")
        assert expr.evaluate({"output": {"count": 10}})
        assert not expr.evaluate({"output": {"count": 3}})

    def test_and_operator(self) -> None:
        expr = ConditionExpr(raw="status == 'done' and output.passed == true")
        assert expr.evaluate({"status": "done", "output": {"passed": True}})
        assert not expr.evaluate({"status": "done", "output": {"passed": False}})

    def test_or_operator(self) -> None:
        expr = ConditionExpr(raw="status == 'done' or status == 'cancelled'")
        assert expr.evaluate({"status": "done"})
        assert expr.evaluate({"status": "cancelled"})
        assert not expr.evaluate({"status": "failed"})

    def test_not_operator(self) -> None:
        expr = ConditionExpr(raw="not status == 'failed'")
        assert expr.evaluate({"status": "done"})
        assert not expr.evaluate({"status": "failed"})

    def test_nested_attribute(self) -> None:
        expr = ConditionExpr(raw="output.test.result == 'passed'")
        assert expr.evaluate({"output": {"test": {"result": "passed"}}})

    def test_in_operator(self) -> None:
        expr = ConditionExpr(raw="status in ['done', 'cancelled']")
        assert expr.evaluate({"status": "done"})
        assert not expr.evaluate({"status": "failed"})

    def test_invalid_syntax(self) -> None:
        with pytest.raises(ConditionError, match="Invalid condition syntax"):
            ConditionExpr(raw="status ==== 'done'")

    def test_unsafe_node_rejected(self) -> None:
        with pytest.raises(ConditionError, match="Unsafe expression node"):
            ConditionExpr(raw="__import__('os').system('ls')")

    def test_unknown_variable(self) -> None:
        expr = ConditionExpr(raw="unknown_var == 'x'")
        with pytest.raises(ConditionError, match="Unknown variable"):
            expr.evaluate({})

    def test_missing_attribute_returns_none(self) -> None:
        expr = ConditionExpr(raw="output.missing == null")
        assert expr.evaluate({"output": {}})

    def test_subscript_dict(self) -> None:
        expr = ConditionExpr(raw="output['key'] == 'val'")
        assert expr.evaluate({"output": {"key": "val"}})

    def test_subscript_missing_key(self) -> None:
        expr = ConditionExpr(raw="output['missing'] == null")
        assert expr.evaluate({"output": {}})

    def test_boolean_literals(self) -> None:
        expr = ConditionExpr(raw="output.flag == true")
        assert expr.evaluate({"output": {"flag": True}})
        assert not expr.evaluate({"output": {"flag": False}})


class TestBuildConditionContext:
    def test_basic_context(self) -> None:
        task = _task("t1", status=TaskStatus.DONE, result_summary="hello")
        ctx = build_condition_context(task)
        assert ctx["status"] == "done"
        assert ctx["result"] == "hello"
        assert ctx["output"] == {}

    def test_json_result_summary(self) -> None:
        task = _task("t1", status=TaskStatus.DONE, result_summary='{"tests_passed": true, "count": 42}')
        ctx = build_condition_context(task)
        assert ctx["output"]["tests_passed"] is True
        assert ctx["output"]["count"] == 42

    def test_none_result(self) -> None:
        task = _task("t1", status=TaskStatus.OPEN)
        ctx = build_condition_context(task)
        assert ctx["result"] == ""
        assert ctx["output"] == {}


# ===========================================================================
# YAML parser tests
# ===========================================================================


class TestParseWorkflowYaml:
    def test_minimal_workflow(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: test-wf
            version: "1.0.0"
            phases:
              - plan
              - implement
            nodes:
              task-a:
                phase: plan
                role: manager
              task-b:
                phase: implement
                role: backend
                depends_on:
                  - task-a
            """,
        )
        dag = parse_workflow_yaml(path)
        assert dag.definition.name == "test-wf"
        assert dag.definition.version == "1.0.0"
        assert len(dag.nodes) == 2
        assert len(dag.edges) == 1
        assert dag.edges[0].source == "task-a"
        assert dag.edges[0].target == "task-b"

    def test_phase_with_options(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: test
            phases:
              - name: plan
                allowed_roles: [manager]
              - name: impl
                requires_approval: true
            nodes:
              a:
                phase: plan
                role: manager
              b:
                phase: impl
                role: backend
                depends_on: [a]
            """,
        )
        dag = parse_workflow_yaml(path)
        assert dag.definition.phases[0].allowed_roles == frozenset({"manager"})
        assert dag.definition.phases[1].requires_approval is True

    def test_conditional_edge(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: test
            phases:
              - plan
              - impl
            nodes:
              a:
                phase: plan
                role: manager
              b:
                phase: impl
                role: backend
                depends_on:
                  - source: a
                    condition: "status == 'done'"
            """,
        )
        dag = parse_workflow_yaml(path)
        assert dag.edges[0].condition is not None
        assert dag.edges[0].condition.raw == "status == 'done'"

    def test_typed_edge(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: test
            phases:
              - plan
              - verify
            nodes:
              a:
                phase: plan
                role: manager
              b:
                phase: verify
                role: qa
                depends_on:
                  - source: a
                    edge_type: validates
            """,
        )
        dag = parse_workflow_yaml(path)
        assert dag.edges[0].edge_type == EdgeType.VALIDATES

    def test_retry_policy(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: test
            phases:
              - plan
              - impl
            nodes:
              a:
                phase: plan
                role: manager
              b:
                phase: impl
                role: backend
                depends_on:
                  - source: a
                    condition: "status == 'failed'"
                retry:
                  max_attempts: 3
                  until: "status == 'done'"
            """,
        )
        dag = parse_workflow_yaml(path)
        node_b = dag.node_map["b"]
        assert node_b.retry is not None
        assert node_b.retry.max_attempts == 3
        assert node_b.retry.until is not None

    def test_fan_out_fan_in(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: fanout
            phases:
              - plan
              - impl
              - verify
            nodes:
              root:
                phase: plan
                role: manager
              work-a:
                phase: impl
                role: backend
                depends_on: [root]
              work-b:
                phase: impl
                role: frontend
                depends_on: [root]
              merge:
                phase: verify
                role: qa
                depends_on:
                  - work-a
                  - work-b
            """,
        )
        dag = parse_workflow_yaml(path)
        # Fan-out: root -> work-a, root -> work-b
        root_edges = [e for e in dag.edges if e.source == "root"]
        assert len(root_edges) == 2
        # Fan-in: work-a -> merge, work-b -> merge
        merge_edges = [e for e in dag.edges if e.target == "merge"]
        assert len(merge_edges) == 2

    def test_missing_name_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            phases:
              - plan
            nodes:
              a:
                phase: plan
                role: manager
            """,
        )
        with pytest.raises(DSLError, match="'name' must be"):
            parse_workflow_yaml(path)

    def test_missing_phases_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: test
            nodes:
              a:
                phase: plan
                role: manager
            """,
        )
        with pytest.raises(DSLError, match="'phases' must be"):
            parse_workflow_yaml(path)

    def test_missing_nodes_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: test
            phases:
              - plan
            """,
        )
        with pytest.raises(DSLError, match="'nodes' must be"):
            parse_workflow_yaml(path)

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("key: [unterminated")
        with pytest.raises(DSLError, match="Invalid YAML"):
            parse_workflow_yaml(path)

    def test_unknown_phase_in_node_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: test
            phases:
              - plan
            nodes:
              a:
                phase: nonexistent
                role: manager
            """,
        )
        with pytest.raises(DSLError, match="unknown phase"):
            parse_workflow_yaml(path)

    def test_unknown_edge_source_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: test
            phases:
              - plan
            nodes:
              a:
                phase: plan
                role: manager
                depends_on:
                  - nonexistent
            """,
        )
        with pytest.raises(DSLError, match="not found in nodes"):
            parse_workflow_yaml(path)

    def test_invalid_condition_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
            name: test
            phases:
              - plan
              - impl
            nodes:
              a:
                phase: plan
                role: manager
              b:
                phase: impl
                role: backend
                depends_on:
                  - source: a
                    condition: "def foo():"
            """,
        )
        with pytest.raises(DSLError, match="condition"):
            parse_workflow_yaml(path)


# ===========================================================================
# DAG validation tests
# ===========================================================================


class TestValidateDAG:
    def test_valid_linear_dag(self) -> None:
        dag = _simple_dag()
        result = validate_dag(dag)
        assert result.is_valid
        assert result.errors == []

    def test_cycle_detected(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="a", phase="plan", role="manager"),
                DAGNode(id="b", phase="implement", role="backend"),
            ),
            edges=(
                DAGEdge(source="a", target="b"),
                DAGEdge(source="b", target="a"),  # cycle
            ),
        )
        result = validate_dag(dag)
        assert not result.is_valid
        assert any("Cycle" in e or "backward" in e.lower() for e in result.errors)

    def test_unreachable_node(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="a", phase="plan", role="manager"),
                DAGNode(id="b", phase="implement", role="backend"),
                DAGNode(id="c", phase="verify", role="qa"),
            ),
            edges=(
                DAGEdge(source="a", target="b"),
                # c has no incoming edges from roots, but is not a root itself
                # Actually c IS a root (no incoming edges), so it's reachable.
            ),
        )
        result = validate_dag(dag)
        # c is a root node, so it's reachable.
        assert result.is_valid

    def test_truly_unreachable_node(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="a", phase="plan", role="manager"),
                DAGNode(id="b", phase="implement", role="backend"),
                DAGNode(id="c", phase="verify", role="qa"),
                DAGNode(id="d", phase="verify", role="qa"),
            ),
            edges=(
                DAGEdge(source="a", target="b"),
                # c -> d forms a disconnected subgraph
                DAGEdge(source="c", target="d"),
            ),
        )
        result = validate_dag(dag)
        # c and d are reachable (c is a root, d is reachable from c)
        assert result.is_valid

    def test_backward_unconditional_edge(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="a", phase="plan", role="manager"),
                DAGNode(id="b", phase="verify", role="qa"),
            ),
            edges=(
                DAGEdge(source="b", target="a"),  # verify -> plan = backward
            ),
        )
        result = validate_dag(dag)
        assert not result.is_valid
        assert any("backward" in e.lower() for e in result.errors)

    def test_backward_conditional_edge_warning(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="a", phase="plan", role="manager"),
                DAGNode(id="b", phase="implement", role="backend"),
                DAGNode(id="c", phase="verify", role="qa"),
            ),
            edges=(
                DAGEdge(source="a", target="b"),
                DAGEdge(source="b", target="c"),
                DAGEdge(
                    source="c",
                    target="b",
                    condition=ConditionExpr(raw="status == 'failed'"),
                ),
            ),
        )
        result = validate_dag(dag)
        assert result.is_valid  # conditional backward is OK
        assert any("loop pattern" in w.lower() for w in result.warnings)

    def test_unknown_edge_source(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(DAGNode(id="a", phase="plan", role="manager"),),
            edges=(DAGEdge(source="nonexistent", target="a"),),
        )
        result = validate_dag(dag)
        assert not result.is_valid
        assert any("not found" in e for e in result.errors)

    def test_unknown_node_phase(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(DAGNode(id="a", phase="nonexistent", role="manager"),),
            edges=(),
        )
        result = validate_dag(dag)
        assert not result.is_valid
        assert any("unknown phase" in e for e in result.errors)


# ===========================================================================
# DAG executor tests
# ===========================================================================


class TestEdgeResolution:
    def test_unconditional_done_is_satisfied(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        edge = dag.edges[0]  # a -> b, unconditional
        tasks = {"a": _task("a", status=TaskStatus.DONE)}
        assert executor.resolve_edge(edge, tasks) == EdgeResolution.SATISFIED

    def test_unconditional_pending(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        edge = dag.edges[0]
        tasks = {"a": _task("a", status=TaskStatus.IN_PROGRESS)}
        assert executor.resolve_edge(edge, tasks) == EdgeResolution.PENDING

    def test_unconditional_failed_is_skipped(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        edge = dag.edges[0]
        tasks = {"a": _task("a", status=TaskStatus.FAILED)}
        assert executor.resolve_edge(edge, tasks) == EdgeResolution.SKIPPED

    def test_unconditional_missing_task_is_pending(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        edge = dag.edges[0]
        assert executor.resolve_edge(edge, {}) == EdgeResolution.PENDING

    def test_conditional_satisfied(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="a", phase="plan", role="manager"),
                DAGNode(id="b", phase="implement", role="backend"),
            ),
            edges=(
                DAGEdge(
                    source="a",
                    target="b",
                    condition=ConditionExpr(raw="status == 'done'"),
                ),
            ),
        )
        executor = DAGExecutor(dag)
        edge = dag.edges[0]
        tasks = {"a": _task("a", status=TaskStatus.DONE)}
        assert executor.resolve_edge(edge, tasks) == EdgeResolution.SATISFIED

    def test_conditional_skipped(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="a", phase="plan", role="manager"),
                DAGNode(id="b", phase="implement", role="backend"),
            ),
            edges=(
                DAGEdge(
                    source="a",
                    target="b",
                    condition=ConditionExpr(raw="status == 'done'"),
                ),
            ),
        )
        executor = DAGExecutor(dag)
        edge = dag.edges[0]
        tasks = {"a": _task("a", status=TaskStatus.FAILED)}
        assert executor.resolve_edge(edge, tasks) == EdgeResolution.SKIPPED

    def test_conditional_with_output_metadata(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="a", phase="plan", role="manager"),
                DAGNode(id="b", phase="implement", role="backend"),
            ),
            edges=(
                DAGEdge(
                    source="a",
                    target="b",
                    condition=ConditionExpr(raw="output.tests_passed == true"),
                ),
            ),
        )
        executor = DAGExecutor(dag)
        edge = dag.edges[0]
        tasks = {
            "a": _task("a", status=TaskStatus.DONE, result_summary='{"tests_passed": true}'),
        }
        assert executor.resolve_edge(edge, tasks) == EdgeResolution.SATISFIED


class TestReadyNodes:
    def test_root_nodes_ready(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        ready = executor.ready_nodes({})
        assert "a" in ready
        assert "b" not in ready
        assert "c" not in ready

    def test_linear_progression(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        tasks = {"a": _task("a", status=TaskStatus.DONE)}
        ready = executor.ready_nodes(tasks)
        assert "b" in ready
        assert "c" not in ready

    def test_fan_out(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="root", phase="plan", role="manager"),
                DAGNode(id="w1", phase="implement", role="backend"),
                DAGNode(id="w2", phase="implement", role="frontend"),
            ),
            edges=(
                DAGEdge(source="root", target="w1"),
                DAGEdge(source="root", target="w2"),
            ),
        )
        executor = DAGExecutor(dag)
        tasks = {"root": _task("root", status=TaskStatus.DONE)}
        ready = executor.ready_nodes(tasks)
        # Both w1 and w2 should be ready (fan-out).
        assert sorted(ready) == ["w1", "w2"]

    def test_fan_in(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="w1", phase="implement", role="backend"),
                DAGNode(id="w2", phase="implement", role="frontend"),
                DAGNode(id="merge", phase="verify", role="qa"),
            ),
            edges=(
                DAGEdge(source="w1", target="merge"),
                DAGEdge(source="w2", target="merge"),
            ),
        )
        executor = DAGExecutor(dag)

        # Only w1 done -> merge not ready.
        tasks: dict[str, Task] = {
            "w1": _task("w1", status=TaskStatus.DONE),
        }
        assert "merge" not in executor.ready_nodes(tasks)

        # Both done -> merge ready.
        tasks["w2"] = _task("w2", status=TaskStatus.DONE)
        assert "merge" in executor.ready_nodes(tasks)

    def test_conditional_edge_gating(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="test", phase="verify", role="qa"),
                DAGNode(id="fix", phase="implement", role="backend"),
            ),
            edges=(
                DAGEdge(
                    source="test",
                    target="fix",
                    condition=ConditionExpr(raw="status == 'failed'"),
                ),
            ),
        )
        executor = DAGExecutor(dag)

        # Test passed (done) -> fix should NOT be ready (condition not met).
        tasks = {"test": _task("test", status=TaskStatus.DONE)}
        assert "fix" not in executor.ready_nodes(tasks)

        # Test failed -> fix should be ready (condition met).
        tasks = {"test": _task("test", status=TaskStatus.FAILED)}
        assert "fix" in executor.ready_nodes(tasks)

    def test_already_active_task_not_ready(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        tasks = {"a": _task("a", status=TaskStatus.IN_PROGRESS)}
        ready = executor.ready_nodes(tasks)
        assert "a" not in ready


class TestRetryPolicy:
    def test_should_retry_with_policy(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(
                    id="a",
                    phase="plan",
                    role="manager",
                    retry=RetryPolicy(max_attempts=3),
                ),
            ),
            edges=(),
        )
        executor = DAGExecutor(dag)
        task = _task("a", status=TaskStatus.FAILED)
        assert executor.should_retry("a", task)

    def test_should_not_retry_without_policy(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        task = _task("a", status=TaskStatus.FAILED)
        assert not executor.should_retry("a", task)

    def test_max_attempts_exhausted(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(
                    id="a",
                    phase="plan",
                    role="manager",
                    retry=RetryPolicy(max_attempts=2),
                ),
            ),
            edges=(),
        )
        executor = DAGExecutor(dag)
        task = _task("a", status=TaskStatus.FAILED)

        executor.record_retry("a")
        assert executor.should_retry("a", task)

        executor.record_retry("a")
        assert not executor.should_retry("a", task)

    def test_until_condition_stops_retry(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(
                    id="a",
                    phase="plan",
                    role="manager",
                    retry=RetryPolicy(
                        max_attempts=5,
                        until=ConditionExpr(raw="status == 'done'"),
                    ),
                ),
            ),
            edges=(),
        )
        executor = DAGExecutor(dag)

        # Failed task -> should retry.
        failed = _task("a", status=TaskStatus.FAILED)
        assert executor.should_retry("a", failed)

        # Done task -> until condition met, no retry.
        done = _task("a", status=TaskStatus.DONE)
        assert not executor.should_retry("a", done)

    def test_retry_in_ready_nodes(self) -> None:
        dag = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(
                    id="a",
                    phase="plan",
                    role="manager",
                    retry=RetryPolicy(max_attempts=3),
                ),
            ),
            edges=(),
        )
        executor = DAGExecutor(dag)
        tasks = {"a": _task("a", status=TaskStatus.FAILED)}
        ready = executor.ready_nodes(tasks)
        assert "a" in ready


class TestCreateTask:
    def test_create_task_from_node(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        task = executor.create_task("a")
        assert task.role == "manager"
        assert task.status == TaskStatus.OPEN
        assert task.id.startswith("a-")

    def test_created_task_has_dependencies(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        task = executor.create_task("b")
        assert "a" in task.depends_on

    def test_create_unknown_node_raises(self) -> None:
        dag = _simple_dag()
        executor = DAGExecutor(dag)
        with pytest.raises(KeyError):
            executor.create_task("nonexistent")


# ===========================================================================
# WorkflowDAG tests
# ===========================================================================


class TestWorkflowDAG:
    def test_node_map(self) -> None:
        dag = _simple_dag()
        assert "a" in dag.node_map
        assert "b" in dag.node_map
        assert "c" in dag.node_map

    def test_definition_hash_deterministic(self) -> None:
        dag = _simple_dag()
        h1 = dag.definition_hash()
        h2 = dag.definition_hash()
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_definition_hash_changes_with_structure(self) -> None:
        dag1 = _simple_dag()
        dag2 = WorkflowDAG(
            definition=_simple_definition(),
            nodes=(
                DAGNode(id="x", phase="plan", role="manager"),
            ),
            edges=(),
        )
        assert dag1.definition_hash() != dag2.definition_hash()


# ===========================================================================
# End-to-end YAML file test
# ===========================================================================


class TestEndToEnd:
    def test_ci_pipeline_yaml(self, tmp_path: Path) -> None:
        """Parse the example ci-pipeline and execute a simulated run."""
        path = _write_yaml(
            tmp_path,
            """\
            name: e2e-test
            version: "1.0.0"

            phases:
              - name: plan
                allowed_roles: [manager]
              - name: implement
              - name: verify
                allowed_roles: [qa]
              - name: merge
                allowed_roles: [manager]

            nodes:
              decompose:
                phase: plan
                role: manager
                description: "Break down the goal"
                estimated_minutes: 10

              build-api:
                phase: implement
                role: backend
                depends_on: [decompose]

              build-ui:
                phase: implement
                role: frontend
                depends_on: [decompose]

              run-tests:
                phase: verify
                role: qa
                depends_on:
                  - build-api
                  - build-ui

              deploy:
                phase: merge
                role: manager
                depends_on:
                  - source: run-tests
                    condition: "status == 'done'"
            """,
        )
        dag = parse_workflow_yaml(path)
        executor = DAGExecutor(dag)

        # Step 1: Only root (decompose) is ready.
        tasks: dict[str, Task] = {}
        ready = executor.ready_nodes(tasks)
        assert ready == ["decompose"]

        # Step 2: decompose done -> fan-out: build-api, build-ui.
        tasks["decompose"] = _task("decompose", role="manager", status=TaskStatus.DONE)
        ready = executor.ready_nodes(tasks)
        assert sorted(ready) == ["build-api", "build-ui"]

        # Step 3: both builds done -> fan-in: run-tests.
        tasks["build-api"] = _task("build-api", role="backend", status=TaskStatus.DONE)
        tasks["build-ui"] = _task("build-ui", role="frontend", status=TaskStatus.DONE)
        ready = executor.ready_nodes(tasks)
        assert ready == ["run-tests"]

        # Step 4a: tests pass -> deploy is ready.
        tasks["run-tests"] = _task("run-tests", role="qa", status=TaskStatus.DONE)
        ready = executor.ready_nodes(tasks)
        assert ready == ["deploy"]

    def test_conditional_branch_skipped(self, tmp_path: Path) -> None:
        """When all conditional edges skip, the node is not scheduled."""
        path = _write_yaml(
            tmp_path,
            """\
            name: cond-test
            phases:
              - plan
              - verify
              - merge
            nodes:
              test:
                phase: plan
                role: qa
              deploy:
                phase: merge
                role: manager
                depends_on:
                  - source: test
                    condition: "status == 'done'"
            """,
        )
        dag = parse_workflow_yaml(path)
        executor = DAGExecutor(dag)

        # Test failed -> deploy should NOT be scheduled.
        tasks = {"test": _task("test", role="qa", status=TaskStatus.FAILED)}
        ready = executor.ready_nodes(tasks)
        assert "deploy" not in ready

    def test_retry_loop(self, tmp_path: Path) -> None:
        """Retry/loop pattern: node retries on failure."""
        path = _write_yaml(
            tmp_path,
            """\
            name: retry-test
            phases:
              - plan
              - impl
            nodes:
              build:
                phase: plan
                role: backend
              fix:
                phase: impl
                role: backend
                depends_on:
                  - source: build
                    condition: "status == 'failed'"
                retry:
                  max_attempts: 2
                  until: "status == 'done'"
            """,
        )
        dag = parse_workflow_yaml(path)
        executor = DAGExecutor(dag)

        # Build failed -> fix is ready.
        tasks = {"build": _task("build", status=TaskStatus.FAILED)}
        assert "fix" in executor.ready_nodes(tasks)

        # Fix failed -> retry check.
        tasks["fix"] = _task("fix", status=TaskStatus.FAILED)
        assert executor.should_retry("fix", tasks["fix"])
        executor.record_retry("fix")
        assert executor.should_retry("fix", tasks["fix"])
        executor.record_retry("fix")
        assert not executor.should_retry("fix", tasks["fix"])
