"""Task dependency graph with critical-path and parallelism analysis.

Builds a DAG from task ``depends_on`` fields and inferred file-overlap
edges, then computes:

* **Critical path** - the longest chain through the DAG (determines
  minimum wall-clock completion time).
* **Parallel width** - the maximum number of independent tasks that
  can run concurrently at any point in the schedule.
* **Bottleneck detection** - surfaces tasks that block the most
  downstream work.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.core.models import Task, TaskStatus

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class EdgeType(StrEnum):
    """Semantic type for task graph edges.

    Controls scheduling and context injection behaviour:

    - ``BLOCKS`` - hard dependency. Successor cannot start until predecessor
      completes.  This is the default for all existing edges.
    - ``INFORMS`` - soft dependency. Predecessor output is available to the
      successor but does **not** block scheduling.
    - ``VALIDATES`` - successor verifies predecessor output. A validator
      failure triggers predecessor retry.  Blocks scheduling like ``BLOCKS``.
    - ``TRANSFORMS`` - predecessor output is input to successor with an
      optional mapping. Does **not** block scheduling.
    """

    BLOCKS = "blocks"
    INFORMS = "informs"
    VALIDATES = "validates"
    TRANSFORMS = "transforms"


# Edge types that prevent a successor from starting until the predecessor
# completes.  INFORMS and TRANSFORMS are non-blocking.
BLOCKING_EDGE_TYPES: frozenset[EdgeType] = frozenset({EdgeType.BLOCKS, EdgeType.VALIDATES})


@dataclass(frozen=True)
class Edge:
    """A directed edge in the task graph."""

    source: str  # dependency (must finish first)
    target: str  # dependent task
    edge_type: str  # origin: "depends_on" or "file_overlap"
    semantic_type: EdgeType = EdgeType.BLOCKS  # scheduling behaviour


@dataclass(frozen=True)
class CycleBreak:
    """A declared dependency cycle the graph had to open to stay schedulable.

    ``edge`` is the edge that was dropped; ``cycle`` names every task on the
    cycle, in order, so an operator can see which declaration to correct.
    """

    edge: Edge
    cycle: tuple[str, ...]


@dataclass
class GraphAnalysis:
    """Results of analysing the task DAG."""

    critical_path: list[str] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    critical_path_minutes: int = 0
    parallel_width: int = 0
    bottlenecks: list[str] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]


# ---------------------------------------------------------------------------
# TaskGraph
# ---------------------------------------------------------------------------


class TaskGraph:
    """DAG built from task dependencies and file-ownership overlaps.

    Nodes are task IDs; edges represent ordering constraints (either
    explicit ``depends_on`` or inferred from shared ``owned_files``).

    The graph is immutable after construction - rebuild it each tick.
    """

    def __init__(self, tasks: Sequence[Task]) -> None:
        self._tasks: dict[str, Task] = {t.id: t for t in tasks}
        # Adjacency: forward (parent → children that depend on it)
        self._forward: dict[str, list[str]] = defaultdict(list)
        # Adjacency: reverse (child → parents it depends on)
        self._reverse: dict[str, list[str]] = defaultdict(list)
        self._edges: list[Edge] = []
        # Reverse lookup: (target) → list of edges pointing into it
        self._edges_by_target: dict[str, list[Edge]] = defaultdict(list)
        # Declared cycles opened during construction, in the order they were found
        self._cycle_breaks: list[CycleBreak] = []

        self._build(tasks)

    # -- Construction -------------------------------------------------------

    def _build(self, tasks: Sequence[Task]) -> None:
        """Populate edges from explicit deps and file overlaps."""
        self._build_explicit_deps(tasks)
        self._build_file_overlap_edges(tasks)
        self._break_declared_cycles()

    def _build_explicit_deps(self, tasks: Sequence[Task]) -> None:
        """Add edges for explicit depends_on relationships."""
        for task in tasks:
            for dep_id in task.depends_on:
                if dep_id in self._tasks:
                    self._add_edge(dep_id, task.id, "depends_on")

    def _build_file_overlap_edges(self, tasks: Sequence[Task]) -> None:
        """Add edges for tasks that share owned files."""
        file_owners: dict[str, list[Task]] = defaultdict(list)
        for task in tasks:
            for f in task.owned_files:
                file_owners[f].append(task)

        for owners in file_owners.values():
            if len(owners) < 2:
                continue
            sorted_owners = sorted(owners, key=lambda t: (t.priority, t.id))
            for i in range(len(sorted_owners) - 1):
                src = sorted_owners[i]
                tgt = sorted_owners[i + 1]
                if tgt.id in self._forward.get(src.id, []):
                    continue
                # The (priority, id) tie-break can point an inferred edge the
                # opposite way to an explicit dependency - a reviewer owning
                # every file its workers touch is the common case. The explicit
                # edge is the authority, so the inferred edge is dropped on
                # purpose rather than flipped: flipping would duplicate an
                # ordering the existing path already expresses.
                if self._reaches(tgt.id, src.id):
                    continue
                self._add_edge(src.id, tgt.id, "file_overlap")

    def _reaches(self, source: str, target: str) -> bool:
        """True if *target* is reachable from *source* along existing edges."""
        if source == target:
            return True
        seen: set[str] = {source}
        stack: list[str] = [source]
        while stack:
            node = stack.pop()
            for child in self._forward.get(node, []):
                if child == target:
                    return True
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return False

    # -- Cycle backstop -----------------------------------------------------

    def _break_declared_cycles(self) -> None:
        """Open every cycle left in the graph so all tasks stay schedulable.

        Inferred file-overlap edges are never allowed to close a cycle, so
        anything found here comes from declared ``depends_on`` data - invalid
        input that used to leave Kahn's algorithm unable to order any task in
        the cycle. Rather than drop those tasks from every ordering for the
        rest of the run, one edge per cycle is removed and recorded.

        The edge chosen is the newest one on the cycle: ``self._edges`` is
        append-only, so its index is the insertion order, and the last edge
        added is the one that closed the cycle. That makes the break
        deterministic - the same board always loses the same edge.

        The cycle is not silenced: each break is logged at ``WARNING`` naming
        the tasks involved, kept on :attr:`cycle_breaks`, and still reported
        by ``DependencyValidator.validate``.
        """
        while (cycle := self._find_cycle()) is not None:
            edge = self._newest_edge_on_cycle(cycle)
            if edge is None:  # pragma: no cover - defensive, a cycle always has edges
                return
            self._remove_edge(edge)
            self._cycle_breaks.append(CycleBreak(edge=edge, cycle=tuple(cycle)))
            logger.warning(
                "Declared dependency cycle %s - dropped its newest edge (%s -> %s, %s) "
                "so the tasks stay schedulable; fix the depends_on declaration",
                " -> ".join([*cycle, cycle[0]]),
                edge.source,
                edge.target,
                edge.edge_type,
            )

    def _find_cycle(self) -> list[str] | None:
        """Return one cycle as an ordered node list, or ``None`` if acyclic.

        Iterative three-colour DFS. Roots are visited in task insertion order
        so that a board with several cycles always yields them in the same
        sequence.
        """
        white, grey, black = 0, 1, 2
        colour: dict[str, int] = dict.fromkeys(self._tasks, white)

        for root in self._tasks:
            if colour[root] != white:
                continue
            path: list[str] = [root]
            colour[root] = grey
            stack: list[tuple[str, Iterator[str]]] = [(root, iter(self._forward.get(root, [])))]
            while stack:
                node, children = stack[-1]
                descended = False
                for child in children:
                    if child not in colour:
                        continue
                    if colour[child] == grey:
                        return path[path.index(child) :]
                    if colour[child] == white:
                        colour[child] = grey
                        path.append(child)
                        stack.append((child, iter(self._forward.get(child, []))))
                        descended = True
                        break
                if not descended:
                    colour[node] = black
                    path.pop()
                    stack.pop()
        return None

    def _newest_edge_on_cycle(self, cycle: list[str]) -> Edge | None:
        """The most recently added edge lying on *cycle*."""
        pairs = {(cycle[i], cycle[(i + 1) % len(cycle)]) for i in range(len(cycle))}
        for edge in reversed(self._edges):
            if (edge.source, edge.target) in pairs:
                return edge
        return None

    def _remove_edge(self, edge: Edge) -> None:
        """Drop *edge* from every adjacency structure that holds it."""
        self._edges.remove(edge)
        self._forward[edge.source].remove(edge.target)
        self._reverse[edge.target].remove(edge.source)
        self._edges_by_target[edge.target].remove(edge)

    def _add_edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        semantic_type: EdgeType = EdgeType.BLOCKS,
    ) -> None:
        self._forward[source].append(target)
        self._reverse[target].append(source)
        edge = Edge(source=source, target=target, edge_type=edge_type, semantic_type=semantic_type)
        self._edges.append(edge)
        self._edges_by_target[target].append(edge)

    def add_dependency(
        self,
        source: str,
        target: str,
        edge_type: EdgeType = EdgeType.BLOCKS,
    ) -> None:
        """Add a typed dependency between two tasks already in the graph.

        This is the public API for adding edges after construction. Use it
        to express richer relationships than the default ``BLOCKS`` edges
        built from ``Task.depends_on``.

        Args:
            source: Predecessor task ID.
            target: Successor task ID.
            edge_type: Semantic relationship (default ``BLOCKS``).

        Raises:
            KeyError: If either task ID is not in the graph.
        """
        if source not in self._tasks:
            raise KeyError(f"Source task {source!r} not in graph")
        if target not in self._tasks:
            raise KeyError(f"Target task {target!r} not in graph")
        self._add_edge(source, target, edge_type="typed", semantic_type=edge_type)

    # -- Queries ------------------------------------------------------------

    @property
    def nodes(self) -> list[str]:
        """All task IDs in the graph."""
        return list(self._tasks)

    @property
    def edges(self) -> list[Edge]:
        """All edges in the graph."""
        return self._edges.copy()

    @property
    def cycle_breaks(self) -> list[CycleBreak]:
        """Declared dependency cycles this graph had to open, if any.

        Empty for a valid board. A non-empty list means the board carries
        invalid ``depends_on`` data that an operator still needs to correct.
        """
        return self._cycle_breaks.copy()

    def dependents(self, task_id: str) -> list[str]:
        """Task IDs that directly depend on *task_id*."""
        return list(self._forward.get(task_id, []))

    def dependencies(self, task_id: str) -> list[str]:
        """Task IDs that *task_id* directly depends on."""
        return list(self._reverse.get(task_id, []))

    def edges_to(self, task_id: str) -> list[Edge]:
        """All incoming edges for *task_id*."""
        return list(self._edges_by_target.get(task_id, []))

    def edges_to_by_type(self, task_id: str, semantic_type: EdgeType) -> list[Edge]:
        """Incoming edges of a specific semantic type."""
        return [e for e in self._edges_by_target.get(task_id, []) if e.semantic_type == semantic_type]

    def validated_by(self, task_id: str) -> list[str]:
        """Task IDs that validate *task_id* (successors via VALIDATES edges)."""
        return [e.target for e in self._edges if e.source == task_id and e.semantic_type == EdgeType.VALIDATES]

    def predecessor_context(self, task_id: str) -> list[dict[str, Any]]:
        """Collect result summaries from INFORMS and TRANSFORMS predecessors.

        Returns a list of dicts with ``task_id``, ``title``,
        ``result_summary``, and ``edge_type`` for each non-blocking
        predecessor that has completed.
        """
        context: list[dict[str, Any]] = []
        for edge in self._edges_by_target.get(task_id, []):
            if edge.semantic_type not in (EdgeType.INFORMS, EdgeType.TRANSFORMS):
                continue
            pred = self._tasks.get(edge.source)
            if pred is None or pred.status != TaskStatus.DONE:
                continue
            context.append(
                {
                    "task_id": pred.id,
                    "title": pred.title,
                    "result_summary": pred.result_summary or "",
                    "edge_type": edge.semantic_type.value,
                }
            )
        return context

    # -- Topological sort ---------------------------------------------------

    def topological_order(self) -> list[str]:
        """Kahn's algorithm - returns [] if cycle detected."""
        in_degree: dict[str, int] = dict.fromkeys(self._tasks, 0)
        for tid in self._tasks:
            for dep in self._forward.get(tid, []):
                if dep in in_degree:
                    in_degree[dep] += 1

        queue: deque[str] = deque(tid for tid, deg in in_degree.items() if deg == 0)
        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for child in self._forward.get(node, []):
                if child in in_degree:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        queue.append(child)

        if len(order) != len(self._tasks):
            # Defensive only: construction opens every cycle it can find, so a
            # stall here means an edge was added after the graph was built.
            logger.warning("Cycle detected in task graph - topological sort incomplete")
            return []
        return order

    # -- Critical path ------------------------------------------------------

    def critical_path(self) -> list[str]:
        """Longest path through the DAG by estimated_minutes.

        Returns the ordered list of task IDs on the critical path.
        An empty list is returned if the graph has a cycle.
        """
        topo = self.topological_order()
        if not topo:
            return []

        dist = self._compute_longest_distances(topo)
        return self._trace_back_path(dist)

    def _compute_longest_distances(self, topo: list[str]) -> dict[str, tuple[int, str | None]]:
        """Compute longest-path distances for every node in topological order."""
        dist: dict[str, tuple[int, str | None]] = {tid: (0, None) for tid in topo}

        for tid in topo:
            if not self._reverse.get(tid):
                dist[tid] = (self._tasks[tid].estimated_minutes, None)

        for node in topo:
            current_dist = dist[node][0]
            for child in self._forward.get(node, []):
                if child not in dist:
                    continue
                new_dist = current_dist + self._tasks[child].estimated_minutes
                if new_dist > dist[child][0]:
                    dist[child] = (new_dist, node)
        return dist

    @staticmethod
    def _trace_back_path(dist: dict[str, tuple[int, str | None]]) -> list[str]:
        """Trace the critical path backwards from the farthest node."""
        if not dist:
            return []
        end_node = max(dist, key=lambda n: dist[n][0])
        if dist[end_node][0] == 0:
            return []
        path: list[str] = []
        current: str | None = end_node
        while current is not None:
            path.append(current)
            current = dist[current][1]
        path.reverse()
        return path

    def critical_path_minutes(self) -> int:
        """Total estimated minutes along the critical path."""
        return sum(self._tasks[tid].estimated_minutes for tid in self.critical_path())

    # -- Parallel width -----------------------------------------------------

    def parallel_width(self) -> int:
        """Maximum number of independent tasks at any scheduling level.

        Uses topological-level assignment: tasks at the same level have
        no ordering constraints between them and can all run in parallel.
        Returns the maximum level width.
        """
        topo = self.topological_order()
        if not topo:
            return len(self._tasks)  # No ordering → everything parallel

        # Level assignment: level of a node = 1 + max(level of parents)
        level: dict[str, int] = {}
        for node in topo:
            parents = self._reverse.get(node, [])
            if not parents:
                level[node] = 0
            else:
                level[node] = 1 + max((level[p] for p in parents if p in level), default=0)

        # Count tasks per level
        level_counts: dict[int, int] = defaultdict(int)
        for lv in level.values():
            level_counts[lv] += 1

        return max(level_counts.values()) if level_counts else 0

    # -- Bottleneck detection -----------------------------------------------

    def bottlenecks(self, *, threshold: int = 2) -> list[str]:
        """Tasks that block at least *threshold* downstream dependents.

        A bottleneck is an in-progress or open task whose transitive
        dependent count meets the threshold.

        Returns task IDs sorted by downstream count (descending).
        """
        blocking_statuses = {TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS}
        candidates = [tid for tid, t in self._tasks.items() if t.status in blocking_statuses]

        downstream_counts: dict[str, int] = {}
        for tid in candidates:
            visited: set[str] = set()
            queue: deque[str] = deque(self._forward.get(tid, []))
            while queue:
                node = queue.popleft()
                if node in visited or node not in self._tasks:
                    continue
                visited.add(node)
                queue.extend(self._forward.get(node, []))
            downstream_counts[tid] = len(visited)

        result = [tid for tid, count in downstream_counts.items() if count >= threshold]
        result.sort(key=lambda tid: downstream_counts[tid], reverse=True)
        return result

    # -- Ready tasks (dependency-aware) -------------------------------------

    def ready_tasks(self) -> list[str]:
        """Task IDs whose *blocking* dependencies are all DONE.

        Only ``BLOCKS`` and ``VALIDATES`` edges prevent a task from being
        ready.  ``INFORMS`` and ``TRANSFORMS`` edges are non-blocking: the
        successor may start even if the predecessor is not yet done.
        """
        done_ids = {tid for tid, t in self._tasks.items() if t.status == TaskStatus.DONE}

        def _blocking_deps_met(tid: str) -> bool:
            """Check that every blocking incoming edge has a done source."""
            for edge in self._edges_by_target.get(tid, []):
                if edge.semantic_type in BLOCKING_EDGE_TYPES and edge.source not in done_ids:
                    return False
            # Also check Task.depends_on for deps not captured as graph
            # edges (e.g. referencing tasks outside this graph).
            task = self._tasks[tid]
            return all(dep in done_ids for dep in task.depends_on if dep not in self._tasks)

        return [tid for tid, t in self._tasks.items() if t.status == TaskStatus.OPEN and _blocking_deps_met(tid)]

    # -- Validation failure handling -----------------------------------------

    def tasks_to_retry_on_validation_failure(self, failed_validator_id: str) -> list[str]:
        """Return task IDs that should be retried when a validator fails.

        When a task connected via a ``VALIDATES`` edge fails, the
        *validated* predecessor should be retried.

        Args:
            failed_validator_id: The task ID of the failed validator.

        Returns:
            List of predecessor task IDs that should be retried.
        """
        return [
            edge.source
            for edge in self._edges_by_target.get(failed_validator_id, [])
            if edge.semantic_type == EdgeType.VALIDATES
        ]

    # -- Full analysis ------------------------------------------------------

    def analyse(self) -> GraphAnalysis:
        """Run all analyses and return a summary."""
        cp = self.critical_path()
        return GraphAnalysis(
            critical_path=cp,
            critical_path_minutes=sum(self._tasks[tid].estimated_minutes for tid in cp),
            parallel_width=self.parallel_width(),
            bottlenecks=self.bottlenecks(),
        )

    # -- Serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise the graph for `.sdd/runtime/task_graph.json`."""
        analysis = self.analyse()
        return {
            "nodes": [
                {
                    "id": t.id,
                    "role": t.role,
                    "status": t.status.value,
                    "estimated_minutes": t.estimated_minutes,
                }
                for t in self._tasks.values()
            ],
            "edges": [
                {
                    "from": e.source,
                    "to": e.target,
                    "type": e.edge_type,
                    "semantic_type": e.semantic_type.value,
                }
                for e in self._edges
            ],
            "critical_path": analysis.critical_path,
            "critical_path_minutes": analysis.critical_path_minutes,
            "parallel_width": analysis.parallel_width,
            "bottlenecks": analysis.bottlenecks,
        }

    def save(self, runtime_dir: Path) -> None:
        """Write the graph JSON to *runtime_dir*/task_graph.json."""
        runtime_dir.mkdir(parents=True, exist_ok=True)
        out = runtime_dir / "task_graph.json"
        out.write_text(json.dumps(self.to_dict(), indent=2))
        logger.debug("Task graph saved to %s", out)
