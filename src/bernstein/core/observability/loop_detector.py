"""Agent loop and deadlock detection.

Detects two failure modes that block agent progress:

Loop detection
--------------
An agent is "looping" when it edits the same file more than
:data:`LOOP_EDIT_THRESHOLD` times within :data:`LOOP_WINDOW_SECONDS`.
This typically happens when an agent is caught in a fix-verify-fail cycle.
Detected agents are killed so the task can be retried or escalated.

Deadlock detection
------------------
Two (or more) agents are "deadlocked" when each holds a file lock that the
other is waiting for.  Detected by building a wait-for graph from:

- ``FileLockManager.all_locks()`` — who holds what
- :meth:`LoopDetector.record_lock_wait` — who is waiting for what

Resolution: release the lock held by the *older* agent (the one that has
been holding its lock the longest), allowing the newer agent to proceed first.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.file_locks import FileLockManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

#: Number of edits to the same file within the window before an agent is
#: flagged as looping.  (>3 means 4 or more edits trigger detection.)
LOOP_EDIT_THRESHOLD: int = 3

#: Sliding window in seconds for edit-count tracking (5 minutes).
LOOP_WINDOW_SECONDS: float = 300.0

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditEvent:
    """A single file-edit event recorded by the loop detector.

    Attributes:
        agent_id: ID of the agent that made the edit.
        file_path: Repository-relative (or absolute) path of the edited file.
        timestamp: Unix timestamp when the edit was recorded.
    """

    agent_id: str
    file_path: str
    timestamp: float


@dataclass(frozen=True)
class LoopDetection:
    """Detected loop for a single (agent, file) pair.

    Attributes:
        agent_id: ID of the looping agent.
        file_path: File being edited repeatedly.
        edit_count: Number of edits recorded within the detection window.
        window_seconds: The detection window used (seconds).
    """

    agent_id: str
    file_path: str
    edit_count: int
    window_seconds: float


@dataclass(frozen=True)
class DeadlockDetection:
    """Detected deadlock among a group of mutually-waiting agents.

    Attributes:
        agents: Agent IDs forming the wait cycle.
        description: Human-readable description of the cycle (e.g. "A → B → A").
        victim_agent_id: The agent whose lock should be released to break the
            deadlock.  Selected as the agent holding the *oldest* lock
            (smallest ``locked_at`` timestamp).
    """

    agents: list[str]
    description: str
    victim_agent_id: str


# ---------------------------------------------------------------------------
# LoopDetector
# ---------------------------------------------------------------------------


class LoopDetector:
    """Tracks file edits and lock waits to detect agent loops and deadlocks.

    Usage::

        detector = LoopDetector()

        # Feed edit events from file-mtime polling or completion data
        detector.record_edit("agent-1", "src/foo.py")

        # Check for loops each orchestrator tick
        for loop in detector.detect_loops():
            kill_agent(loop.agent_id)

        # Record when a task batch is deferred due to lock conflicts
        detector.record_lock_wait(
            waiting_agent_id="agent-2",
            wanted_files=["src/foo.py"],
            held_by={"src/foo.py": "agent-1"},
        )

        # Check for deadlocks each tick
        for dl in detector.detect_deadlocks(lock_mgr):
            lock_mgr.release(dl.victim_agent_id)
    """

    def __init__(self) -> None:
        # Chronological list of edit events; pruned on every detect_loops() call.
        self._edits: list[EditEvent] = []

        # wait_for[agent_id] = set of file paths this agent is blocked on.
        self._wait_for: dict[str, set[str]] = defaultdict(set)

        # Mapping of file_path -> {holder_agent_id: lock_acquired_ts} for
        # victim selection during deadlock resolution.
        self._lock_ts: dict[str, dict[str, float]] = defaultdict(dict)

    # ------------------------------------------------------------------
    # Loop detection
    # ------------------------------------------------------------------

    def record_edit(
        self,
        agent_id: str,
        file_path: str,
        ts: float | None = None,
    ) -> None:
        """Record that *agent_id* modified *file_path*.

        Args:
            agent_id: The agent that performed the edit.
            file_path: The file that was modified.
            ts: Unix timestamp of the edit.  Defaults to ``time.time()``.
        """
        self._edits.append(
            EditEvent(
                agent_id=agent_id,
                file_path=file_path,
                timestamp=ts if ts is not None else time.time(),
            )
        )

    def detect_loops(
        self,
        *,
        threshold: int = LOOP_EDIT_THRESHOLD,
        window_seconds: float = LOOP_WINDOW_SECONDS,
    ) -> list[LoopDetection]:
        """Return agents that have edited the same file more than *threshold* times.

        Prunes stale events outside *window_seconds* before counting, so this
        method is safe to call every tick.

        Args:
            threshold: Number of edits that triggers loop detection (default 3,
                meaning >3 = 4 or more within the window).
            window_seconds: Sliding window for edit counts in seconds (default
                5 minutes).

        Returns:
            One :class:`LoopDetection` per (agent, file) pair that exceeds the
            threshold.  Empty list when no loops are detected.
        """
        cutoff = time.time() - window_seconds
        # Prune events outside the window in-place
        self._edits = [e for e in self._edits if e.timestamp >= cutoff]

        counts: dict[tuple[str, str], int] = defaultdict(int)
        for event in self._edits:
            counts[(event.agent_id, event.file_path)] += 1

        return [
            LoopDetection(
                agent_id=agent_id,
                file_path=file_path,
                edit_count=count,
                window_seconds=window_seconds,
            )
            for (agent_id, file_path), count in counts.items()
            if count > threshold
        ]

    # ------------------------------------------------------------------
    # Deadlock detection
    # ------------------------------------------------------------------

    def record_lock_wait(
        self,
        waiting_agent_id: str,
        wanted_files: list[str],
        held_by: dict[str, str],
        *,
        lock_timestamps: dict[str, float] | None = None,
    ) -> None:
        """Record that *waiting_agent_id* is blocked waiting for *wanted_files*.

        Call this whenever a task batch is deferred because at least one of its
        requested files is locked by another agent.

        Args:
            waiting_agent_id: The agent (or prospective agent ID) that cannot
                proceed.
            wanted_files: File paths this agent needs but cannot acquire.
            held_by: Mapping of ``{file_path: holder_agent_id}`` for each
                conflicting file.
            lock_timestamps: Optional ``{holder_agent_id: lock_acquired_ts}``
                mapping for victim selection.  When omitted the current time is
                stored as a conservative upper bound.
        """
        ts_map = lock_timestamps or {}
        for file_path in wanted_files:
            holder = held_by.get(file_path)
            if holder and holder != waiting_agent_id:
                self._wait_for[waiting_agent_id].add(file_path)
                holder_ts = ts_map.get(holder, time.time())
                if file_path not in self._lock_ts.get(holder, {}):
                    self._lock_ts[holder][file_path] = holder_ts

    def clear_wait(self, agent_id: str) -> None:
        """Remove all wait-for entries for *agent_id*.

        Call this when an agent acquires its locks, exits, or is killed so
        stale wait entries do not generate phantom deadlock reports.

        Args:
            agent_id: Agent whose wait-for entries to remove.
        """
        self._wait_for.pop(agent_id, None)
        self._lock_ts.pop(agent_id, None)

    def detect_deadlocks(
        self,
        lock_mgr: FileLockManager,
    ) -> list[DeadlockDetection]:
        """Find cycles in the agent wait-for graph.

        A deadlock is a cycle: agent A is waiting for a file held by agent B,
        and agent B is waiting for a file held by agent A (generalises to N
        agents).

        Victim selection: the agent in the cycle holding the *oldest* lock
        (smallest ``locked_at`` timestamp) is chosen as the victim, so the
        other agent can proceed first.

        Args:
            lock_mgr: Active :class:`~bernstein.core.file_locks.FileLockManager`
                for the current orchestrator run.

        Returns:
            One :class:`DeadlockDetection` per distinct deadlock cycle.  Empty
            list when no deadlocks are detected.
        """
        # Build: file_path → (holder_agent_id, locked_at)
        held: dict[str, tuple[str, float]] = {}
        for lock in lock_mgr.all_locks():
            held[lock.file_path] = (lock.agent_id, lock.locked_at)

        # Build wait-for graph: waiting_agent → set of blocking agents
        waits_for: dict[str, set[str]] = defaultdict(set)
        for waiting_agent, wanted_files in self._wait_for.items():
            for file_path in wanted_files:
                if file_path in held:
                    holder_agent, _ = held[file_path]
                    if holder_agent != waiting_agent:
                        waits_for[waiting_agent].add(holder_agent)

        # Detect cycles via DFS
        cycles = _find_cycles(dict(waits_for))

        results: list[DeadlockDetection] = []
        seen: set[frozenset[str]] = set()
        for cycle in cycles:
            key = frozenset(cycle)
            if key in seen:
                continue
            seen.add(key)

            victim = _oldest_lock_holder(cycle, held)
            desc = " → ".join([*cycle, cycle[0]])
            results.append(
                DeadlockDetection(
                    agents=list(cycle),
                    description=f"Deadlock: {desc}",
                    victim_agent_id=victim,
                )
            )

        return results


# ---------------------------------------------------------------------------
# Private graph helpers
# ---------------------------------------------------------------------------


def _all_graph_nodes(graph: dict[str, set[str]]) -> set[str]:
    """Collect every node referenced in *graph* (keys + values).

    Args:
        graph: Adjacency mapping.

    Returns:
        Set of all node IDs.
    """
    nodes: set[str] = set(graph)
    for neighbors in graph.values():
        nodes.update(neighbors)
    return nodes


def _dfs_cycles_from(start: str, graph: dict[str, set[str]], visited: set[str]) -> list[list[str]]:
    """Run iterative DFS from *start* to find simple cycles back to it.

    Args:
        start: Starting node for cycle search.
        graph: Adjacency mapping.
        visited: Globally visited nodes (not re-entered as start).

    Returns:
        Cycles found starting/ending at *start*.
    """
    cycles: list[list[str]] = []
    stack: list[tuple[str, list[str], set[str]]] = [(start, [start], {start})]
    while stack:
        node, path, path_set = stack.pop()
        for neighbor in sorted(graph.get(node, set())):
            if neighbor == start and len(path) > 1:
                cycles.append(list(path))
            elif neighbor not in path_set and neighbor not in visited:
                stack.append((neighbor, [*path, neighbor], path_set | {neighbor}))
    return cycles


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find all simple cycles in *graph* using iterative DFS.

    Each returned cycle is an ordered list of node IDs representing one pass
    around the loop (the repeated start node is implied, not included).

    Args:
        graph: Adjacency mapping ``{node: {successor, ...}}``.

    Returns:
        List of cycles, each as an ordered list of node IDs.
    """
    all_nodes = _all_graph_nodes(graph)
    cycles: list[list[str]] = []
    visited: set[str] = set()

    for start in sorted(all_nodes):  # sorted for determinism
        if start in visited:
            continue
        cycles.extend(_dfs_cycles_from(start, graph, visited))
        visited.add(start)

    return cycles


def _oldest_lock_holder(
    cycle: list[str],
    held: dict[str, tuple[str, float]],
) -> str:
    """Return the agent in *cycle* with the oldest (earliest) lock.

    The agent holding the oldest lock is the victim: releasing its lock breaks
    the cycle and lets other agents proceed.

    Falls back to ``cycle[0]`` when no lock timestamp information is available.

    Args:
        cycle: Agent IDs forming the deadlock cycle.
        held: Mapping of ``{file_path: (agent_id, locked_at)}``.

    Returns:
        The agent ID chosen as the victim.
    """
    agent_min_ts: dict[str, float] = {}
    for _, (agent_id, locked_at) in held.items():
        if agent_id in cycle and (agent_id not in agent_min_ts or locked_at < agent_min_ts[agent_id]):
            agent_min_ts[agent_id] = locked_at

    if not agent_min_ts:
        return cycle[0]

    return min(agent_min_ts, key=lambda a: agent_min_ts[a])
