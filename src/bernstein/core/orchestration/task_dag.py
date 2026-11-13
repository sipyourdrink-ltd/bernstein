"""Task DAG with explicit parallel-safety and user-story grouping.

This module loads a hand-authored task DAG file and walks it in
dependency order while preserving each task's declarative
``parallel_safe`` flag (set at task-generation time, not inferred
from file overlap).

File formats
------------

Two on-disk shapes are supported:

1. **Markdown checkbox list** - one task per line::

       - [ ] [T001] [P] [US1] Add YAML loader
       - [ ] [T002]    [US1] Wire loader into orchestrator -> T001
       - [ ] [T003] [P] [US2] Render parallel batches

   ``[T<id>]`` is required.  ``[P]`` opts the task into parallel
   scheduling.  ``[US<n>]`` groups tasks into a user-story slice for
   rollback.  Dependencies use the trailing ``-> T002, T003`` arrow.

2. **YAML** - a list of task dicts under ``tasks:``::

       tasks:
         - id: T001
           description: Add YAML loader
           parallel_safe: true
           story_id: US1
         - id: T002
           description: Wire loader into orchestrator
           depends_on: [T001]
           story_id: US1

Walking
-------

:func:`topological_iter_with_parallel` yields one frozenset per "batch"
of tasks ready to run.  A batch contains the tasks whose dependencies
are already complete and that share the ``parallel_safe`` flag - when a
batch contains a serial task, it is yielded alone.  Cycles raise
:class:`TaskDagCycleError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.tasks.backlog_parser import ParsedTaskLine, parse_task_lines

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class TaskDagError(Exception):
    """Base for task DAG load/validation errors."""


class TaskDagCycleError(TaskDagError):
    """Raised when the task DAG has tasks that cannot be scheduled.

    Attributes:
        remaining: IDs of tasks still pending when the traversal stalled.
    """

    def __init__(self, remaining: list[str]) -> None:
        self.remaining = remaining
        super().__init__("Task DAG has a dependency cycle involving: " + ", ".join(sorted(remaining)))


@dataclass(frozen=True)
class TaskNode:
    """One node in the task DAG.

    Attributes:
        task_id: Stable identifier from the ``[T<id>]`` marker.
        description: Human-readable task description.
        parallel_safe: True when the planner has declared this task safe
            to run alongside other parallel-safe siblings.  Defaults to
            False so absence is conservative.
        story_id: Optional user-story slice for rollback grouping.
        depends_on: IDs of tasks that must complete first.
    """

    task_id: str
    description: str
    parallel_safe: bool = False
    story_id: str | None = None
    depends_on: tuple[str, ...] = ()


@dataclass
class TaskDag:
    """A directed acyclic graph of :class:`TaskNode` objects.

    Attributes:
        nodes: Task nodes indexed by ID. Each graph starts with an empty
            mapping unless one is supplied.
    """

    nodes: dict[str, TaskNode] = field(default_factory=dict)

    # ── Construction ────────────────────────────────────────────────

    @classmethod
    def from_nodes(cls, nodes: list[TaskNode]) -> TaskDag:
        """Build a DAG from an ordered list of nodes."""
        out: dict[str, TaskNode] = {}
        for node in nodes:
            if node.task_id in out:
                raise TaskDagError(f"Duplicate task id: {node.task_id}")
            out[node.task_id] = node
        dag = cls(nodes=out)
        dag._validate_dependencies()
        return dag

    @classmethod
    def from_markdown(cls, content: str) -> TaskDag:
        """Load a DAG from a markdown checkbox list."""
        parsed: list[ParsedTaskLine] = parse_task_lines(content)
        nodes = [
            TaskNode(
                task_id=line.task_id,
                description=line.description,
                parallel_safe=line.parallel_safe,
                story_id=line.story_id,
                depends_on=tuple(line.depends_on),
            )
            for line in parsed
        ]
        return cls.from_nodes(nodes)

    @classmethod
    def from_yaml(cls, content: str) -> TaskDag:
        """Load a DAG from a YAML document with a ``tasks:`` list."""
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise TaskDagError("PyYAML is required to load YAML task DAG files") from exc
        loaded = yaml.safe_load(content) or {}
        if not isinstance(loaded, dict):
            raise TaskDagError("YAML task DAG root must be a mapping")
        raw_tasks = loaded.get("tasks", [])
        if not isinstance(raw_tasks, list):
            raise TaskDagError("YAML task DAG 'tasks' must be a list")

        nodes: list[TaskNode] = []
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                raise TaskDagError("Each task entry must be a mapping")
            task_id = str(raw.get("id", "")).strip()
            if not task_id:
                raise TaskDagError("Every task entry must declare 'id'")
            deps_raw = raw.get("depends_on", [])
            if not isinstance(deps_raw, list):
                raise TaskDagError(f"{task_id}: depends_on must be a list")
            nodes.append(
                TaskNode(
                    task_id=task_id,
                    description=str(raw.get("description", "")).strip(),
                    parallel_safe=bool(raw.get("parallel_safe", False)),
                    story_id=(str(raw["story_id"]).strip() if raw.get("story_id") else None),
                    depends_on=tuple(str(d).strip() for d in deps_raw if str(d).strip()),
                )
            )
        return cls.from_nodes(nodes)

    @classmethod
    def from_path(cls, path: Path) -> TaskDag:
        """Auto-detect format from file extension and load."""
        text = path.read_text(encoding="utf-8")
        suffix = path.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            return cls.from_yaml(text)
        return cls.from_markdown(text)

    # ── Queries ──────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self) -> Iterator[TaskNode]:
        return iter(self.nodes.values())

    def get(self, task_id: str) -> TaskNode | None:
        """Return the node with ``task_id`` or ``None``."""
        return self.nodes.get(task_id)

    def stories(self) -> dict[str, list[TaskNode]]:
        """Group nodes by ``story_id`` (skipping nodes without one)."""
        out: dict[str, list[TaskNode]] = {}
        for node in self.nodes.values():
            if node.story_id is None:
                continue
            out.setdefault(node.story_id, []).append(node)
        return out

    # ── Validation ───────────────────────────────────────────────────

    def _validate_dependencies(self) -> None:
        for node in self.nodes.values():
            for dep in node.depends_on:
                if dep not in self.nodes:
                    raise TaskDagError(f"Task {node.task_id} depends on unknown task {dep}")


def topological_iter_with_parallel(dag: TaskDag) -> Iterator[frozenset[TaskNode]]:
    """Walk ``dag`` yielding batches of tasks ready to run.

    A batch is a frozen set of :class:`TaskNode` objects whose
    dependencies are already complete.  Batches preserve the planner's
    parallel-safety declaration:

    * If at least one ready task has ``parallel_safe = False`` we yield
      that task alone (and yield each serial task individually).
    * If every ready task has ``parallel_safe = True`` we yield them
      together as a single concurrent batch.

    Args:
        dag: Task graph to traverse.

    Raises:
        TaskDagCycleError: If tasks remain but none of their outstanding
            dependencies can be satisfied by the traversal.
    """
    pending: dict[str, TaskNode] = dag.nodes.copy()
    completed: set[str] = set()

    while pending:
        ready = [n for n in pending.values() if all(d in completed for d in n.depends_on)]
        if not ready:
            raise TaskDagCycleError(remaining=list(pending))

        # Stable ordering for deterministic output.
        ready.sort(key=lambda n: n.task_id)

        if all(n.parallel_safe for n in ready) and len(ready) > 1:
            yield frozenset(ready)
            for n in ready:
                completed.add(n.task_id)
                pending.pop(n.task_id, None)
        else:
            # Yield serial tasks one at a time; parallel-safe nodes in
            # this mixed batch wait for the next iteration where they
            # may form a pure parallel batch.
            serial = next((n for n in ready if not n.parallel_safe), ready[0])
            yield frozenset({serial})
            completed.add(serial.task_id)
            pending.pop(serial.task_id, None)


def format_plan(dag: TaskDag) -> str:
    """Render a DAG walk as a human-readable plan.

    Used by the ``bernstein tasks dag --file`` CLI to highlight which
    batches will execute in parallel.
    """
    lines: list[str] = []
    lines.append(f"Task DAG: {len(dag)} task(s)")
    for batch_idx, batch in enumerate(topological_iter_with_parallel(dag), start=1):
        tasks = sorted(batch, key=lambda n: n.task_id)
        marker = "[PARALLEL]" if len(tasks) > 1 else "[SERIAL]  "
        lines.append(f"  Batch {batch_idx:>2} {marker} ({len(tasks)} task(s))")
        for node in tasks:
            story = f" {node.story_id}" if node.story_id else ""
            lines.append(f"    - {node.task_id}{story}: {node.description}")
    stories = dag.stories()
    if stories:
        lines.extend(("", "User-story rollback groups:"))
        for story_id, members in sorted(stories.items()):
            ids = ", ".join(n.task_id for n in members)
            lines.append(f"  {story_id}: {ids}")
    return "\n".join(lines)
