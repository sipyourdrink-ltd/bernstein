"""Deterministic run reproducibility tests.

Verifies that the orchestrator produces identical scheduling, dependency
resolution, role assignment, and model selection given the same inputs.
These are pure logic tests — no server, no agents, no IO.
"""

from __future__ import annotations

from bernstein.core.graph import TaskGraph
from bernstein.core.models import Complexity, ModelConfig, Scope, Task, TaskStatus, TaskType
from bernstein.core.router import route_task
from bernstein.core.tick_pipeline import group_by_role

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
    complexity: Complexity = Complexity.MEDIUM,
    scope: Scope = Scope.MEDIUM,
    task_type: TaskType = TaskType.STANDARD,
    model: str | None = None,
    effort: str | None = None,
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description=f"Description for {id}",
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        status=TaskStatus(status),
        depends_on=depends_on or [],
        owned_files=owned_files or [],
        estimated_minutes=estimated_minutes,
        task_type=task_type,
        model=model,
        effort=effort,
    )


# ---------------------------------------------------------------------------
# Test: same tasks, same scheduling order
# ---------------------------------------------------------------------------


class TestSameTasksSameOrder:
    """Given the same set of tasks, group_by_role produces identical batches."""

    def test_same_tasks_same_order(self) -> None:
        """Run group_by_role 10 times with the same inputs; order must be identical."""
        tasks = [
            _t(id="T-01", role="backend", priority=1),
            _t(id="T-02", role="backend", priority=2),
            _t(id="T-03", role="qa", priority=1),
            _t(id="T-04", role="qa", priority=3),
            _t(id="T-05", role="frontend", priority=2),
            _t(id="T-06", role="frontend", priority=1),
            _t(id="T-07", role="backend", priority=3),
            _t(id="T-08", role="security", priority=2),
        ]

        reference = group_by_role(tasks, max_per_batch=2)
        reference_ids = [[t.id for t in batch] for batch in reference]

        for i in range(9):
            result = group_by_role(tasks, max_per_batch=2)
            result_ids = [[t.id for t in batch] for batch in result]
            assert result_ids == reference_ids, (
                f"Run {i + 2} produced different order: {result_ids} vs {reference_ids}"
            )

    def test_same_order_with_alive_per_role(self) -> None:
        """Starving-role reordering is also deterministic."""
        tasks = [
            _t(id="T-01", role="backend", priority=1),
            _t(id="T-02", role="qa", priority=1),
            _t(id="T-03", role="frontend", priority=2),
        ]
        alive = {"backend": 2, "qa": 0, "frontend": 1}

        reference = group_by_role(tasks, max_per_batch=1, alive_per_role=alive)
        reference_ids = [[t.id for t in b] for b in reference]

        for i in range(9):
            result = group_by_role(tasks, max_per_batch=1, alive_per_role=alive)
            result_ids = [[t.id for t in b] for b in result]
            assert result_ids == reference_ids, (
                f"Run {i + 2} with alive_per_role differed: {result_ids} vs {reference_ids}"
            )

    def test_same_order_with_file_affinity(self) -> None:
        """File-affinity grouping produces deterministic batches."""
        tasks = [
            _t(id="T-01", role="backend", priority=2, owned_files=["src/a.py"]),
            _t(id="T-02", role="backend", priority=2, owned_files=["src/a.py", "src/b.py"]),
            _t(id="T-03", role="backend", priority=1, owned_files=["src/c.py"]),
        ]

        reference = group_by_role(tasks, max_per_batch=3)
        reference_ids = [[t.id for t in b] for b in reference]

        for i in range(9):
            result = group_by_role(tasks, max_per_batch=3)
            result_ids = [[t.id for t in b] for b in result]
            assert result_ids == reference_ids, (
                f"Run {i + 2} with file affinity differed: {result_ids} vs {reference_ids}"
            )


# ---------------------------------------------------------------------------
# Test: dependency resolution (topological sort) deterministic
# ---------------------------------------------------------------------------


class TestDependencyResolutionDeterministic:
    """Topological sort of a task DAG must be stable across runs."""

    def test_linear_chain(self) -> None:
        """A -> B -> C always produces [A, B, C]."""
        tasks = [
            _t(id="A", depends_on=[]),
            _t(id="B", depends_on=["A"]),
            _t(id="C", depends_on=["B"]),
        ]
        for _ in range(10):
            g = TaskGraph(tasks)
            assert g.topological_order() == ["A", "B", "C"]

    def test_diamond_dag(self) -> None:
        """Diamond: A -> {B, C} -> D.  Order must be stable."""
        tasks = [
            _t(id="A"),
            _t(id="B", depends_on=["A"]),
            _t(id="C", depends_on=["A"]),
            _t(id="D", depends_on=["B", "C"]),
        ]

        reference = TaskGraph(tasks).topological_order()
        assert reference[0] == "A"
        assert reference[-1] == "D"

        for i in range(9):
            order = TaskGraph(tasks).topological_order()
            assert order == reference, (
                f"Run {i + 2}: {order} != {reference}"
            )

    def test_wide_dag_stable_tiebreak(self) -> None:
        """Many independent tasks should produce a stable ordering."""
        tasks = [_t(id=f"T-{i:02d}") for i in range(20)]

        reference = TaskGraph(tasks).topological_order()

        for i in range(9):
            order = TaskGraph(tasks).topological_order()
            assert order == reference, (
                f"Run {i + 2} wide DAG order diverged"
            )

    def test_complex_dag(self) -> None:
        """Multi-layer DAG with mixed fan-in/fan-out is deterministic."""
        tasks = [
            _t(id="root"),
            _t(id="L1-a", depends_on=["root"]),
            _t(id="L1-b", depends_on=["root"]),
            _t(id="L1-c", depends_on=["root"]),
            _t(id="L2-a", depends_on=["L1-a", "L1-b"]),
            _t(id="L2-b", depends_on=["L1-b", "L1-c"]),
            _t(id="L3-sink", depends_on=["L2-a", "L2-b"]),
        ]

        reference = TaskGraph(tasks).topological_order()
        assert reference[0] == "root"
        assert reference[-1] == "L3-sink"

        for i in range(9):
            order = TaskGraph(tasks).topological_order()
            assert order == reference, (
                f"Run {i + 2} complex DAG order diverged"
            )

    def test_ready_tasks_deterministic(self) -> None:
        """ready_tasks() returns the same set in the same order every time."""
        tasks = [
            _t(id="done-1", status="done"),
            _t(id="done-2", status="done"),
            _t(id="open-a", depends_on=["done-1"]),
            _t(id="open-b", depends_on=["done-2"]),
            _t(id="open-c", depends_on=["done-1", "done-2"]),
            _t(id="blocked", depends_on=["open-a"]),
        ]

        reference = TaskGraph(tasks).ready_tasks()

        for i in range(9):
            ready = TaskGraph(tasks).ready_tasks()
            assert ready == reference, (
                f"Run {i + 2}: ready_tasks diverged: {ready} vs {reference}"
            )


# ---------------------------------------------------------------------------
# Test: role assignment deterministic
# ---------------------------------------------------------------------------


class TestRoleAssignmentDeterministic:
    """Tasks with roles produce identical per-role grouping every time."""

    def test_role_grouping_consistent(self) -> None:
        """group_by_role partitions tasks by role deterministically."""
        tasks = [
            _t(id="T-01", role="backend", priority=2),
            _t(id="T-02", role="qa", priority=1),
            _t(id="T-03", role="frontend", priority=3),
            _t(id="T-04", role="backend", priority=1),
            _t(id="T-05", role="qa", priority=2),
            _t(id="T-06", role="security", priority=2),
        ]

        reference = group_by_role(tasks, max_per_batch=1)
        ref_roles = [(b[0].role, b[0].id) for b in reference]

        for i in range(9):
            result = group_by_role(tasks, max_per_batch=1)
            result_roles = [(b[0].role, b[0].id) for b in result]
            assert result_roles == ref_roles, (
                f"Run {i + 2} role grouping diverged: {result_roles} vs {ref_roles}"
            )

    def test_role_round_robin_deterministic(self) -> None:
        """Round-robin interleaving across roles is stable.

        With backend(3 tasks) + qa(2 tasks), the interleaved sequence
        must be identical across runs.
        """
        tasks = [
            _t(id="B-1", role="backend", priority=1),
            _t(id="B-2", role="backend", priority=2),
            _t(id="B-3", role="backend", priority=3),
            _t(id="Q-1", role="qa", priority=1),
            _t(id="Q-2", role="qa", priority=2),
        ]

        reference = group_by_role(tasks, max_per_batch=1)
        ref_seq = [(b[0].role, b[0].id) for b in reference]

        for i in range(9):
            result = group_by_role(tasks, max_per_batch=1)
            result_seq = [(b[0].role, b[0].id) for b in result]
            assert result_seq == ref_seq, (
                f"Run {i + 2} round-robin diverged: {result_seq} vs {ref_seq}"
            )


# ---------------------------------------------------------------------------
# Test: model selection deterministic
# ---------------------------------------------------------------------------


class TestModelSelectionDeterministic:
    """route_task produces the same ModelConfig every time (no randomness)."""

    def test_critical_priority_always_opus(self) -> None:
        """Priority-1 tasks always route to opus/max."""
        task = _t(id="T-crit", priority=1, role="backend")
        for _ in range(10):
            cfg = route_task(task)
            assert cfg.model == "opus"
            assert cfg.effort == "max"

    def test_manager_role_always_opus(self) -> None:
        """Manager role always routes to opus/max."""
        task = _t(id="T-mgr", role="manager")
        for _ in range(10):
            cfg = route_task(task)
            assert cfg.model == "opus"
            assert cfg.effort == "max"

    def test_security_role_always_opus(self) -> None:
        """Security role always routes to opus/max."""
        task = _t(id="T-sec", role="security")
        for _ in range(10):
            cfg = route_task(task)
            assert cfg.model == "opus"
            assert cfg.effort == "max"

    def test_large_scope_always_opus(self) -> None:
        """Large-scope tasks always route to opus/max."""
        task = _t(id="T-large", scope=Scope.LARGE, role="backend")
        for _ in range(10):
            cfg = route_task(task)
            assert cfg.model == "opus"
            assert cfg.effort == "max"

    def test_high_complexity_heuristic_fallback(self) -> None:
        """High complexity (no bandit) falls back to sonnet/high deterministically."""
        task = _t(id="T-hi", complexity=Complexity.HIGH, role="backend", priority=2)
        for _ in range(10):
            cfg = route_task(task)
            assert cfg.model == "sonnet"
            assert cfg.effort == "high"

    def test_default_route_deterministic(self) -> None:
        """Default routing (medium complexity, normal priority) is stable."""
        task = _t(id="T-default", role="backend", priority=2, complexity=Complexity.MEDIUM)
        reference = route_task(task)
        for _ in range(9):
            cfg = route_task(task)
            assert cfg.model == reference.model
            assert cfg.effort == reference.effort

    def test_manager_override_deterministic(self) -> None:
        """Manager-specified model/effort overrides are deterministic."""
        task = _t(id="T-override", role="backend", model="haiku", effort="low")
        for _ in range(10):
            cfg = route_task(task)
            assert cfg.model == "haiku"
            assert cfg.effort == "low"

    def test_different_tasks_get_consistent_configs(self) -> None:
        """A batch of distinct tasks produces the same configs every time."""
        tasks = [
            _t(id="T-a", role="backend", priority=2, complexity=Complexity.LOW),
            _t(id="T-b", role="qa", priority=1),
            _t(id="T-c", role="manager"),
            _t(id="T-d", role="frontend", priority=3, scope=Scope.SMALL),
            _t(id="T-e", role="security", complexity=Complexity.HIGH),
        ]

        reference = [(route_task(t).model, route_task(t).effort) for t in tasks]

        for run in range(9):
            configs = [(route_task(t).model, route_task(t).effort) for t in tasks]
            assert configs == reference, (
                f"Run {run + 2}: model configs diverged: {configs} vs {reference}"
            )
