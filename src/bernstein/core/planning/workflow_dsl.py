"""Workflow DSL — declarative YAML/Python for conditional task DAGs.

Extends governed workflow mode (``WorkflowDefinition``) with user-authored
conditional DAGs.  DAG nodes map to workflow phases; edges carry guard
predicates that compose with lifecycle transition guards.

DSL files live in ``.bernstein/workflows/`` and are loaded by name.

Example YAML::

    name: ci-pipeline
    version: "1.0.0"

    phases:
      - name: plan
        allowed_roles: [manager, architect]
      - name: implement
        requires_approval: true
      - name: verify
        allowed_roles: [qa, security]
      - name: merge
        allowed_roles: [manager]

    nodes:
      decompose:
        phase: plan
        role: manager
        description: "Break down the goal"

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

      fix-bugs:
        phase: implement
        role: backend
        depends_on:
          - source: run-tests
            condition: "status == 'failed'"
        retry:
          max_attempts: 3
          until: "status == 'done'"

      deploy:
        phase: merge
        role: manager
        depends_on:
          - source: run-tests
            condition: "status == 'done'"
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import yaml

from bernstein.core.knowledge.task_graph import EdgeType
from bernstein.core.models import Scope, Task, TaskStatus
from bernstein.core.planning.workflow import WorkflowDefinition, WorkflowPhase

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DSLError(Exception):
    """Raised for invalid workflow DSL files."""


class ConditionError(Exception):
    """Raised when a condition expression is invalid or evaluation fails."""


# ---------------------------------------------------------------------------
# Condition expression evaluator (safe, no eval())
# ---------------------------------------------------------------------------

# AST node types allowed in condition expressions.
_SAFE_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.Compare,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Attribute,
    ast.Name,
    ast.Constant,
    ast.Subscript,
    ast.List,
    ast.Tuple,
    ast.Load,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)


def _validate_ast_safety(node: ast.AST) -> None:
    """Reject any AST node not in the safe list."""
    if not isinstance(node, _SAFE_NODES):
        msg = f"Unsafe expression node: {type(node).__name__}"
        raise ConditionError(msg)
    for child in ast.iter_child_nodes(node):
        _validate_ast_safety(child)


_YAML_BUILTINS: dict[str, Any] = {"true": True, "false": False, "null": None}


def _eval_name(node: ast.Name, ctx: dict[str, Any]) -> Any:
    """Evaluate an ast.Name node against the context."""
    if node.id in ctx:
        return ctx[node.id]
    if node.id in _YAML_BUILTINS:
        return _YAML_BUILTINS[node.id]
    msg = f"Unknown variable: {node.id!r}"
    raise ConditionError(msg)


def _eval_subscript(node: ast.Subscript, ctx: dict[str, Any]) -> Any:
    """Evaluate an ast.Subscript node."""
    obj = _eval_ast(node.value, ctx)
    key = _eval_ast(node.slice, ctx)
    if isinstance(obj, (dict, list, tuple)):
        try:
            return obj[key]  # type: ignore[index]
        except (KeyError, IndexError):
            return None
    return None


def _eval_compare(node: ast.Compare, ctx: dict[str, Any]) -> bool:
    """Evaluate an ast.Compare node."""
    left = _eval_ast(node.left, ctx)
    for op, comparator in zip(node.ops, node.comparators, strict=True):
        right = _eval_ast(comparator, ctx)
        if not _compare(op, left, right):
            return False
        left = right
    return True


def _eval_ast(node: ast.AST, ctx: dict[str, Any]) -> Any:
    """Recursively evaluate a pre-validated AST against *ctx*."""
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, ctx)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return _eval_name(node, ctx)
    if isinstance(node, ast.Attribute):
        obj = _eval_ast(node.value, ctx)
        return obj.get(node.attr) if isinstance(obj, dict) else getattr(obj, node.attr, None)
    if isinstance(node, ast.Subscript):
        return _eval_subscript(node, ctx)
    if isinstance(node, ast.List):
        return [_eval_ast(elt, ctx) for elt in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_ast(elt, ctx) for elt in node.elts)
    if isinstance(node, ast.Compare):
        return _eval_compare(node, ctx)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_ast(v, ctx) for v in node.values)
        return any(_eval_ast(v, ctx) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_ast(node.operand, ctx)

    msg = f"Cannot evaluate node: {type(node).__name__}"
    raise ConditionError(msg)


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    """Evaluate a single comparison operator."""
    if isinstance(op, ast.Eq):
        return left == right  # type: ignore[no-any-return]
    if isinstance(op, ast.NotEq):
        return left != right  # type: ignore[no-any-return]
    if isinstance(op, ast.Lt):
        return left < right  # type: ignore[no-any-return]
    if isinstance(op, ast.LtE):
        return left <= right  # type: ignore[no-any-return]
    if isinstance(op, ast.Gt):
        return left > right  # type: ignore[no-any-return]
    if isinstance(op, ast.GtE):
        return left >= right  # type: ignore[no-any-return]
    if isinstance(op, ast.In):
        return left in right  # type: ignore[no-any-return]
    if isinstance(op, ast.NotIn):
        return left not in right  # type: ignore[no-any-return]
    msg = f"Unsupported comparison: {type(op).__name__}"
    raise ConditionError(msg)


@dataclass(frozen=True)
class ConditionExpr:
    """A safe expression evaluated against task output metadata.

    Supported syntax:
        - Field access: ``output.field``, ``status``
        - Comparisons: ``==``, ``!=``, ``>``, ``<``, ``>=``, ``<=``
        - Logical: ``and``, ``or``, ``not``
        - Membership: ``in``, ``not in``
        - Literals: strings, numbers, booleans, None

    Attributes:
        raw: The original expression string.
    """

    raw: str

    def __post_init__(self) -> None:
        # Validate at construction time.
        tree = _parse_condition(self.raw)
        _validate_ast_safety(tree)
        # Store parsed AST as a "hidden" attribute on frozen dataclass.
        object.__setattr__(self, "_tree", tree)

    def evaluate(self, context: dict[str, Any]) -> bool:
        """Evaluate the condition against *context*.

        Args:
            context: Dict with keys like ``status``, ``output``, ``result``.

        Returns:
            True if the condition holds.
        """
        tree: ast.Expression = object.__getattribute__(self, "_tree")
        result = _eval_ast(tree, context)
        return bool(result)


def _parse_condition(raw: str) -> ast.Expression:
    """Parse a condition string into a validated AST."""
    try:
        return ast.parse(raw, mode="eval")
    except SyntaxError as exc:
        msg = f"Invalid condition syntax: {raw!r} — {exc}"
        raise ConditionError(msg) from exc


def build_condition_context(task: Task) -> dict[str, Any]:
    """Build evaluation context from a Task for condition expressions.

    The context exposes:
        - ``status``: task status value (string)
        - ``result``: task result_summary (string or None)
        - ``output``: dict parsed from result_summary if JSON, else empty
    """
    output: dict[str, Any] = {}
    if task.result_summary:
        try:
            parsed = json.loads(task.result_summary)
            if isinstance(parsed, dict):
                output = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "status": task.status.value,
        "result": task.result_summary or "",
        "output": output,
    }


# ---------------------------------------------------------------------------
# DAG data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Retry/loop configuration for a DAG node.

    Attributes:
        max_attempts: Maximum retries (1 = no retry).
        until: Condition that terminates the retry loop (evaluated against
            the node's own task output).
    """

    max_attempts: int = 1
    until: ConditionExpr | None = None


@dataclass(frozen=True)
class DAGNode:
    """A task template node in the workflow DAG.

    Attributes:
        id: Unique node identifier.
        phase: Workflow phase this node belongs to.
        role: Specialist role for the task (e.g. "backend", "qa").
        description: Human-readable description.
        estimated_minutes: Time estimate for scheduling.
        retry: Optional retry/loop configuration.
    """

    id: str
    phase: str
    role: str
    description: str = ""
    estimated_minutes: int = 30
    retry: RetryPolicy | None = None


@dataclass(frozen=True)
class DAGEdge:
    """A directed edge with optional guard condition.

    Attributes:
        source: Upstream node ID.
        target: Downstream node ID.
        condition: Guard predicate; None means unconditional.
        edge_type: Semantic edge type (blocks, informs, etc.).
    """

    source: str
    target: str
    condition: ConditionExpr | None = None
    edge_type: EdgeType = EdgeType.BLOCKS


@dataclass(frozen=True)
class WorkflowDAG:
    """Declarative workflow DAG extending WorkflowDefinition.

    Combines governed phase structure with a conditional task DAG.
    Nodes map to phases; edges carry guard predicates that compose with
    lifecycle transition guards.

    Attributes:
        definition: The underlying governed workflow (phases, approvals).
        nodes: Ordered tuple of task template nodes.
        edges: Directed edges (some conditional) between nodes.
    """

    definition: WorkflowDefinition
    nodes: tuple[DAGNode, ...]
    edges: tuple[DAGEdge, ...]

    @property
    def node_map(self) -> dict[str, DAGNode]:
        """Node ID → DAGNode lookup."""
        return {n.id: n for n in self.nodes}

    def definition_hash(self) -> str:
        """SHA-256 hash covering both phases and DAG structure."""
        phase_hash = self.definition.definition_hash()
        dag_payload = json.dumps(
            {
                "nodes": [{"id": n.id, "phase": n.phase, "role": n.role} for n in self.nodes],
                "edges": [
                    {
                        "source": e.source,
                        "target": e.target,
                        "condition": e.condition.raw if e.condition else None,
                        "edge_type": e.edge_type.value,
                    }
                    for e in self.edges
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        combined = f"{phase_hash}:{dag_payload}"
        return hashlib.sha256(combined.encode()).hexdigest()


# ---------------------------------------------------------------------------
# YAML parser
# ---------------------------------------------------------------------------


def parse_workflow_yaml(path: Path) -> WorkflowDAG:
    """Parse a workflow DSL YAML file into a WorkflowDAG.

    Args:
        path: Path to the YAML file.

    Returns:
        Validated WorkflowDAG.

    Raises:
        DSLError: On parse or validation failure.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"Cannot read workflow file: {exc}"
        raise DSLError(msg) from exc

    try:
        data: object = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        msg = f"Invalid YAML: {exc}"
        raise DSLError(msg) from exc

    if not isinstance(data, dict):
        msg = f"Workflow file must be a YAML mapping, got {type(data).__name__}"
        raise DSLError(msg)

    name = data.get("name")
    if not name or not isinstance(name, str):
        msg = "'name' must be a non-empty string"
        raise DSLError(msg)

    version = str(data.get("version", "1.0.0"))

    # --- Parse phases ---
    phases_raw = data.get("phases")
    if not phases_raw or not isinstance(phases_raw, list):
        msg = "'phases' must be a non-empty list"
        raise DSLError(msg)
    phases = _parse_phases(phases_raw)

    # --- Parse nodes and edges ---
    nodes_raw = data.get("nodes")
    if not nodes_raw or not isinstance(nodes_raw, dict):
        msg = "'nodes' must be a non-empty mapping"
        raise DSLError(msg)

    nodes = _parse_nodes(nodes_raw)
    edges = _parse_edges_from_nodes(nodes_raw)

    definition = WorkflowDefinition(name=name, phases=phases, version=version)
    dag = WorkflowDAG(definition=definition, nodes=nodes, edges=edges)

    # Validate and raise on errors.
    result = validate_dag(dag)
    if not result.is_valid:
        msg = "Workflow DAG validation failed:\n  " + "\n  ".join(result.errors)
        raise DSLError(msg)

    for warning in result.warnings:
        logger.warning("Workflow DSL warning: %s", warning)

    return dag


def _parse_phases(raw: list[Any]) -> tuple[WorkflowPhase, ...]:
    """Parse phase definitions from YAML."""
    phases: list[WorkflowPhase] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            phases.append(WorkflowPhase(name=item))
            continue
        if not isinstance(item, dict):
            msg = f"phases[{i}]: must be a string or mapping"
            raise DSLError(msg)

        pname = item.get("name")
        if not pname or not isinstance(pname, str):
            msg = f"phases[{i}]: 'name' is required"
            raise DSLError(msg)

        roles_raw = item.get("allowed_roles", [])
        if not isinstance(roles_raw, list):
            msg = f"phases[{i}]: 'allowed_roles' must be a list"
            raise DSLError(msg)

        phases.append(
            WorkflowPhase(
                name=pname,
                allowed_roles=frozenset(str(r) for r in roles_raw),
                requires_approval=bool(item.get("requires_approval", False)),
                entry_guard_description=str(item.get("entry_guard", "")),
            )
        )

    return tuple(phases)


def _parse_nodes(raw: dict[str, Any]) -> tuple[DAGNode, ...]:
    """Parse DAG node definitions from YAML."""
    nodes: list[DAGNode] = []
    for node_id, spec in raw.items():
        if not isinstance(spec, dict):
            msg = f"nodes.{node_id}: must be a mapping"
            raise DSLError(msg)

        phase = spec.get("phase")
        if not phase or not isinstance(phase, str):
            msg = f"nodes.{node_id}: 'phase' is required"
            raise DSLError(msg)

        role = spec.get("role")
        if not role or not isinstance(role, str):
            msg = f"nodes.{node_id}: 'role' is required"
            raise DSLError(msg)

        retry: RetryPolicy | None = None
        retry_raw = spec.get("retry")
        if retry_raw is not None:
            if not isinstance(retry_raw, dict):
                msg = f"nodes.{node_id}.retry: must be a mapping"
                raise DSLError(msg)
            retry = _parse_retry(node_id, retry_raw)

        nodes.append(
            DAGNode(
                id=str(node_id),
                phase=phase,
                role=role,
                description=str(spec.get("description", "")),
                estimated_minutes=int(spec.get("estimated_minutes", 30)),
                retry=retry,
            )
        )

    return tuple(nodes)


def _parse_retry(node_id: str, raw: dict[str, Any]) -> RetryPolicy:
    """Parse a retry policy from YAML."""
    max_attempts = int(raw.get("max_attempts", 1))
    if max_attempts < 1:
        msg = f"nodes.{node_id}.retry.max_attempts: must be >= 1"
        raise DSLError(msg)

    until: ConditionExpr | None = None
    until_raw = raw.get("until")
    if until_raw is not None:
        try:
            until = ConditionExpr(raw=str(until_raw))
        except ConditionError as exc:
            msg = f"nodes.{node_id}.retry.until: {exc}"
            raise DSLError(msg) from exc

    return RetryPolicy(max_attempts=max_attempts, until=until)


def _parse_dict_dep(dep: dict[str, Any], node_id: str, index: int) -> DAGEdge:
    """Parse a conditional or typed dependency mapping into a DAGEdge.

    Args:
        dep: The dependency mapping.
        node_id: Parent node ID (for error messages).
        index: Index in the depends_on list (for error messages).

    Returns:
        A DAGEdge.

    Raises:
        DSLError: If the mapping is malformed.
    """
    source = dep.get("source")
    if not source or not isinstance(source, str):
        msg = f"nodes.{node_id}.depends_on[{index}]: 'source' is required"
        raise DSLError(msg)

    condition: ConditionExpr | None = None
    cond_raw = dep.get("condition")
    if cond_raw is not None:
        try:
            condition = ConditionExpr(raw=str(cond_raw))
        except ConditionError as exc:
            msg = f"nodes.{node_id}.depends_on[{index}].condition: {exc}"
            raise DSLError(msg) from exc

    edge_type_raw = dep.get("edge_type", "blocks")
    try:
        edge_type = EdgeType(edge_type_raw)
    except ValueError:
        msg = (
            f"nodes.{node_id}.depends_on[{index}].edge_type: "
            f"unknown type {edge_type_raw!r}, "
            f"expected one of {[e.value for e in EdgeType]}"
        )
        raise DSLError(msg) from None

    return DAGEdge(source=source, target=str(node_id), condition=condition, edge_type=edge_type)


def _parse_edges_from_nodes(raw: dict[str, Any]) -> tuple[DAGEdge, ...]:
    """Extract edges from node depends_on fields."""
    edges: list[DAGEdge] = []
    for node_id, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        deps = spec.get("depends_on", [])
        if not isinstance(deps, list):
            msg = f"nodes.{node_id}.depends_on: must be a list"
            raise DSLError(msg)

        for i, dep in enumerate(deps):
            if isinstance(dep, str):
                edges.append(DAGEdge(source=dep, target=str(node_id)))
            elif isinstance(dep, dict):
                edges.append(_parse_dict_dep(dep, str(node_id), i))
            else:
                msg = f"nodes.{node_id}.depends_on[{i}]: must be a string or mapping"
                raise DSLError(msg)

    return tuple(edges)


# ---------------------------------------------------------------------------
# DAG validation
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of DAG validation.

    Attributes:
        errors: Fatal issues that prevent execution.
        warnings: Non-fatal issues the user should be aware of.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """True when there are no errors."""
        return len(self.errors) == 0


def validate_dag(dag: WorkflowDAG) -> ValidationResult:
    """Validate a WorkflowDAG for structural correctness.

    Checks:
        1. No cycles in the unconditional edge subgraph.
        2. All nodes are reachable from at least one root.
        3. Node phases exist in the workflow definition.
        4. No backward phase dependencies on unconditional edges.
        5. All edge source/target node IDs exist.
        6. Condition expressions parse correctly.

    Args:
        dag: The WorkflowDAG to validate.

    Returns:
        ValidationResult with errors and warnings.
    """
    result = ValidationResult()
    node_ids = {n.id for n in dag.nodes}
    phase_names = dag.definition.phase_names()

    # Check edge references.
    for edge in dag.edges:
        if edge.source not in node_ids:
            result.errors.append(f"Edge source {edge.source!r} not found in nodes")
        if edge.target not in node_ids:
            result.errors.append(f"Edge target {edge.target!r} not found in nodes")

    if result.errors:
        return result  # Can't proceed with broken references.

    # Check node phases.
    for node in dag.nodes:
        if node.phase not in phase_names:
            result.errors.append(
                f"Node {node.id!r} references unknown phase {node.phase!r}; known phases: {phase_names}"
            )

    if result.errors:
        return result

    node_map = dag.node_map
    phase_index = {name: i for i, name in enumerate(phase_names)}

    result.errors.extend(_check_phase_ordering(dag.edges, node_map, phase_index))
    result.errors.extend(_check_cycles(dag))
    result.warnings.extend(_check_reachability(dag))
    result.warnings.extend(_check_conditional_backward_edges(dag.edges, node_map, phase_index))

    return result


def _check_phase_ordering(
    edges: tuple[DAGEdge, ...],
    node_map: dict[str, Any],
    phase_index: dict[str, int],
) -> list[str]:
    """Return errors for unconditional edges that go backward in phase ordering."""
    errors: list[str] = []
    for edge in edges:
        if edge.condition is not None:
            continue
        src_phase = phase_index[node_map[edge.source].phase]
        tgt_phase = phase_index[node_map[edge.target].phase]
        if tgt_phase < src_phase:
            errors.append(
                f"Unconditional backward edge: {edge.source!r} (phase "
                f"{node_map[edge.source].phase!r}) -> {edge.target!r} (phase "
                f"{node_map[edge.target].phase!r})"
            )
    return errors


def _check_conditional_backward_edges(
    edges: tuple[DAGEdge, ...],
    node_map: dict[str, Any],
    phase_index: dict[str, int],
) -> list[str]:
    """Return warnings for conditional edges that go backward (loop patterns)."""
    warnings: list[str] = []
    for edge in edges:
        if edge.condition is None:
            continue
        src_phase = phase_index[node_map[edge.source].phase]
        tgt_phase = phase_index[node_map[edge.target].phase]
        if tgt_phase < src_phase:
            warnings.append(
                f"Conditional backward edge (loop pattern): "
                f"{edge.source!r} -> {edge.target!r} "
                f"(condition: {edge.condition.raw!r})"
            )
    return warnings


def _check_cycles(dag: WorkflowDAG) -> list[str]:
    """Check for cycles in the unconditional edge subgraph using Kahn's algorithm."""
    node_ids = {n.id for n in dag.nodes}

    # Build adjacency for unconditional edges only.
    forward: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = dict.fromkeys(node_ids, 0)
    for edge in dag.edges:
        if edge.condition is not None:
            continue
        forward[edge.source].append(edge.target)
        in_degree[edge.target] += 1

    queue: deque[str] = deque(nid for nid, deg in in_degree.items() if deg == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in forward.get(node, []):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if visited < len(node_ids):
        stuck = [nid for nid, deg in in_degree.items() if deg > 0]
        return [f"Cycle detected involving nodes: {stuck}"]
    return []


def _check_reachability(dag: WorkflowDAG) -> list[str]:
    """Check that all nodes are reachable from at least one root."""
    node_ids = {n.id for n in dag.nodes}
    has_incoming: set[str] = set()
    forward: dict[str, list[str]] = defaultdict(list)

    for edge in dag.edges:
        has_incoming.add(edge.target)
        forward[edge.source].append(edge.target)

    roots = node_ids - has_incoming
    if not roots:
        return ["No root nodes found (every node has incoming edges)"]

    # BFS from all roots.
    reachable: set[str] = set()
    queue: deque[str] = deque(roots)
    while queue:
        node = queue.popleft()
        if node in reachable:
            continue
        reachable.add(node)
        for child in forward.get(node, []):
            if child not in reachable:
                queue.append(child)

    unreachable = node_ids - reachable
    if unreachable:
        return [f"Unreachable nodes (no path from roots): {sorted(unreachable)}"]
    return []


# ---------------------------------------------------------------------------
# Edge resolution & DAG executor
# ---------------------------------------------------------------------------


class EdgeResolution(StrEnum):
    """How an edge was resolved for scheduling."""

    SATISFIED = "satisfied"  # Upstream done and condition (if any) is true.
    SKIPPED = "skipped"  # Upstream terminal but condition is false.
    PENDING = "pending"  # Upstream not yet terminal.


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED})


class DAGExecutor:
    """Drives task scheduling through a conditional DAG.

    Works alongside ``WorkflowExecutor`` — phases gate which nodes are
    eligible; the ``DAGExecutor`` resolves conditional edges within those
    phases.

    Args:
        dag: The workflow DAG to execute.
    """

    def __init__(self, dag: WorkflowDAG) -> None:
        self._dag = dag
        self._node_map = dag.node_map
        # Edges grouped by target.
        self._edges_by_target: dict[str, list[DAGEdge]] = defaultdict(list)
        for edge in dag.edges:
            self._edges_by_target[edge.target].append(edge)
        # Track retry counts per node.
        self._retry_counts: dict[str, int] = defaultdict(int)

    @property
    def dag(self) -> WorkflowDAG:
        """The underlying WorkflowDAG."""
        return self._dag

    def resolve_edge(self, edge: DAGEdge, tasks: dict[str, Task]) -> EdgeResolution:
        """Determine the resolution state of a single edge.

        Args:
            edge: The DAGEdge to resolve.
            tasks: Map of node_id -> Task for the current run.

        Returns:
            EdgeResolution for this edge.
        """
        source_task = tasks.get(edge.source)
        if source_task is None or source_task.status not in TERMINAL_STATUSES:
            return EdgeResolution.PENDING

        if edge.condition is None:
            # Unconditional edge: satisfied when source is DONE.
            if source_task.status == TaskStatus.DONE:
                return EdgeResolution.SATISFIED
            return EdgeResolution.SKIPPED

        # Conditional edge: evaluate guard predicate.
        ctx = build_condition_context(source_task)
        try:
            if edge.condition.evaluate(ctx):
                return EdgeResolution.SATISFIED
        except ConditionError:
            logger.warning(
                "Condition evaluation failed for edge %s -> %s: %r",
                edge.source,
                edge.target,
                edge.condition.raw,
                exc_info=True,
            )
        return EdgeResolution.SKIPPED

    def _is_node_eligible(self, node_id: str, existing: Task | None) -> bool:
        """Check if a node with resolved deps is eligible for task creation."""
        if existing is not None and existing.status == TaskStatus.FAILED:
            return self.should_retry(node_id, existing)
        return existing is None

    def _are_deps_resolved(self, incoming: list[DAGEdge], tasks: dict[str, Task]) -> bool:
        """Return True if all incoming edges are resolved and at least one is satisfied."""
        any_satisfied = False
        for edge in incoming:
            resolution = self.resolve_edge(edge, tasks)
            if resolution == EdgeResolution.PENDING:
                return False
            if resolution == EdgeResolution.SATISFIED:
                any_satisfied = True
        return any_satisfied

    def ready_nodes(self, tasks: dict[str, Task]) -> list[str]:
        """Return node IDs whose dependencies are fully resolved.

        A node is ready when:
            1. All incoming edges are either SATISFIED or SKIPPED.
            2. At least one incoming edge is SATISFIED.
            3. The node doesn't already have an active task.

        Root nodes (no incoming edges) are always ready if they have no
        active task.

        Args:
            tasks: Map of node_id -> Task for active/completed tasks.

        Returns:
            List of node IDs ready for task creation.
        """
        ready: list[str] = []

        for node in self._dag.nodes:
            existing = tasks.get(node.id)
            if existing is not None and existing.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                continue

            incoming = self._edges_by_target.get(node.id, [])
            if not incoming:
                if self._is_node_eligible(node.id, existing):
                    ready.append(node.id)
                continue

            if self._are_deps_resolved(incoming, tasks) and self._is_node_eligible(node.id, existing):
                ready.append(node.id)

        return ready

    def should_retry(self, node_id: str, task: Task) -> bool:
        """Check if a failed task should be retried per the node's retry policy.

        Args:
            node_id: The DAG node ID.
            task: The failed Task.

        Returns:
            True if the task should be retried.
        """
        node = self._node_map.get(node_id)
        if node is None or node.retry is None:
            return False

        count = self._retry_counts.get(node_id, 0)
        if count >= node.retry.max_attempts:
            return False

        # If there's an "until" condition and it's met, don't retry.
        if node.retry.until is not None:
            ctx = build_condition_context(task)
            try:
                if node.retry.until.evaluate(ctx):
                    return False
            except ConditionError:
                pass

        return True

    def record_retry(self, node_id: str) -> None:
        """Increment the retry counter for a node."""
        self._retry_counts[node_id] = self._retry_counts.get(node_id, 0) + 1

    def create_task(self, node_id: str) -> Task:
        """Create a Task from a DAG node template.

        Args:
            node_id: The DAG node ID to instantiate.

        Returns:
            A new Task ready for scheduling.

        Raises:
            KeyError: If the node_id is not in the DAG.
        """
        node = self._node_map[node_id]
        task_id = f"{node.id}-{uuid.uuid4().hex[:8]}"

        # Collect unconditional blocking dependencies.
        dep_ids: list[str] = []
        for edge in self._edges_by_target.get(node.id, []):
            if edge.condition is None and edge.edge_type in {EdgeType.BLOCKS, EdgeType.VALIDATES}:
                dep_ids.append(edge.source)

        return Task(
            id=task_id,
            title=node.description or f"DAG node: {node.id}",
            description=node.description,
            role=node.role,
            priority=2,
            scope=Scope.MEDIUM,
            estimated_minutes=node.estimated_minutes,
            status=TaskStatus.OPEN,
            depends_on=dep_ids,
            created_at=time.time(),
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_workflow_dsl(
    name: str,
    search_dir: Path | None = None,
) -> WorkflowDAG | None:
    """Load a workflow DSL file by name.

    Searches ``.bernstein/workflows/`` for ``{name}.yaml`` or ``{name}.yml``.

    Args:
        name: Workflow name (without extension).
        search_dir: Override the search directory (default: ``.bernstein/workflows/``).

    Returns:
        Parsed WorkflowDAG, or None if not found.
    """
    from pathlib import Path as _Path

    if search_dir is None:
        search_dir = _Path(".bernstein") / "workflows"

    for ext in (".yaml", ".yml"):
        candidate = search_dir / f"{name}{ext}"
        if candidate.is_file():
            return parse_workflow_yaml(candidate)

    return None
