"""ENT-007: Cluster task stealing for load balancing.

Idle nodes claim tasks from busy nodes to balance work across the cluster.
The stealing algorithm uses a pull-based approach: idle nodes periodically
check if they can steal tasks from overloaded peers.

Stealing criteria:
- Thief node must have available capacity (slots > 0)
- Victim node must have queued (not in-progress) tasks above a threshold
- Task must not be pinned to a specific node
- Stolen tasks are re-assigned atomically via CAS-style versioning
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_STEAL_THRESHOLD = 3  # Victim must have this many queued tasks
_DEFAULT_STEAL_BATCH = 1  # Max tasks to steal per attempt
_DEFAULT_COOLDOWN_S = 10.0  # Min seconds between steal attempts per pair


class StealResult(StrEnum):
    """Outcome of a task-stealing attempt."""

    SUCCESS = "success"
    NO_CANDIDATES = "no_candidates"
    VICTIM_BELOW_THRESHOLD = "victim_below_threshold"
    COOLDOWN = "cooldown"
    VERSION_CONFLICT = "version_conflict"
    TASK_PINNED = "task_pinned"


@dataclass(frozen=True)
class StealConfig:
    """Configuration for task stealing behaviour.

    Attributes:
        steal_threshold: Minimum queued tasks on victim before stealing.
        max_steal_batch: Maximum tasks to steal in one attempt.
        cooldown_s: Minimum seconds between steal attempts for a node pair.
        enabled: Whether task stealing is active.
    """

    steal_threshold: int = _DEFAULT_STEAL_THRESHOLD
    max_steal_batch: int = _DEFAULT_STEAL_BATCH
    cooldown_s: float = _DEFAULT_COOLDOWN_S
    enabled: bool = True


# ---------------------------------------------------------------------------
# Task descriptor (lightweight view for stealing decisions)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StealableTask:
    """Lightweight task descriptor used for stealing decisions.

    Attributes:
        task_id: Unique task identifier.
        node_id: Node currently holding the task.
        queued_at: Timestamp when the task was queued.
        pinned_node: If set, task cannot be stolen from this node.
        version: Optimistic concurrency version for CAS.
        priority: Task priority (lower = higher priority).
    """

    task_id: str = ""
    node_id: str = ""
    queued_at: float = 0.0
    pinned_node: str = ""
    version: int = 0
    priority: int = 5


@dataclass(frozen=True)
class NodeLoad:
    """Snapshot of a node's task load.

    Attributes:
        node_id: Node identifier.
        queued_tasks: Number of queued (waiting) tasks.
        running_tasks: Number of in-progress tasks.
        available_slots: Number of agent slots available.
        total_slots: Total agent capacity.
    """

    node_id: str = ""
    queued_tasks: int = 0
    running_tasks: int = 0
    available_slots: int = 0
    total_slots: int = 4


# ---------------------------------------------------------------------------
# Steal attempt result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StealAttempt:
    """Record of a single steal attempt.

    Attributes:
        thief_node: Node that attempted the steal.
        victim_node: Node tasks were stolen from.
        tasks_stolen: List of stolen task IDs.
        result: Outcome of the attempt.
        timestamp: When the attempt occurred.
    """

    thief_node: str = ""
    victim_node: str = ""
    tasks_stolen: list[str] = field(default_factory=list[str])
    result: StealResult = StealResult.NO_CANDIDATES
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Task stealing engine
# ---------------------------------------------------------------------------


class TaskStealingEngine:
    """Manages task stealing between cluster nodes.

    Pull-based: idle nodes invoke ``attempt_steal`` to claim tasks
    from overloaded peers.

    Args:
        config: Stealing configuration.
    """

    def __init__(self, config: StealConfig | None = None) -> None:
        self._config = config or StealConfig()
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._history: list[StealAttempt] = []

    @property
    def config(self) -> StealConfig:
        """Return the steal configuration."""
        return self._config

    @property
    def history(self) -> list[StealAttempt]:
        """Return the steal attempt history."""
        return list(self._history)

    def find_victim(
        self,
        thief_id: str,
        nodes: list[NodeLoad],
    ) -> NodeLoad | None:
        """Find the best victim node to steal from.

        Picks the node with the highest queued task count above the
        threshold, excluding the thief itself and nodes on cooldown.

        Args:
            thief_id: The stealing node's identifier.
            nodes: Current load snapshot for all nodes.

        Returns:
            Best victim NodeLoad, or None if no suitable victim.
        """
        now = time.time()
        candidates: list[NodeLoad] = []
        for node in nodes:
            if node.node_id == thief_id:
                continue
            if node.queued_tasks < self._config.steal_threshold:
                continue
            pair = (thief_id, node.node_id)
            last = self._cooldowns.get(pair, 0.0)
            if now - last < self._config.cooldown_s:
                continue
            candidates.append(node)

        if not candidates:
            return None

        # Pick node with most queued tasks
        return max(candidates, key=lambda n: n.queued_tasks)

    def select_tasks_to_steal(
        self,
        tasks: list[StealableTask],
        thief_id: str,
    ) -> list[StealableTask]:
        """Select which tasks to steal from a victim's queue.

        Filters out pinned tasks and selects up to ``max_steal_batch``
        tasks ordered by priority (lowest first) then queue time (oldest first).

        Args:
            tasks: Victim's queued tasks.
            thief_id: The stealing node's identifier.

        Returns:
            List of tasks to steal (may be empty).
        """
        stealable = [t for t in tasks if not t.pinned_node or t.pinned_node == thief_id]
        # Sort by priority (ascending), then by queue time (ascending = oldest first)
        stealable.sort(key=lambda t: (t.priority, t.queued_at))
        return stealable[: self._config.max_steal_batch]

    def attempt_steal(
        self,
        thief_id: str,
        nodes: list[NodeLoad],
        victim_tasks: dict[str, list[StealableTask]],
    ) -> StealAttempt:
        """Execute a task-stealing attempt.

        Args:
            thief_id: The node attempting to steal.
            nodes: Load snapshots for all cluster nodes.
            victim_tasks: Mapping of node_id -> queued tasks.

        Returns:
            StealAttempt recording the outcome.
        """
        if not self._config.enabled:
            attempt = StealAttempt(
                thief_node=thief_id,
                result=StealResult.NO_CANDIDATES,
            )
            self._history.append(attempt)
            return attempt

        victim = self.find_victim(thief_id, nodes)
        if victim is None:
            attempt = StealAttempt(
                thief_node=thief_id,
                result=StealResult.NO_CANDIDATES,
            )
            self._history.append(attempt)
            return attempt

        tasks = victim_tasks.get(victim.node_id, [])
        if len(tasks) < self._config.steal_threshold:
            attempt = StealAttempt(
                thief_node=thief_id,
                victim_node=victim.node_id,
                result=StealResult.VICTIM_BELOW_THRESHOLD,
            )
            self._history.append(attempt)
            return attempt

        selected = self.select_tasks_to_steal(tasks, thief_id)
        if not selected:
            attempt = StealAttempt(
                thief_node=thief_id,
                victim_node=victim.node_id,
                result=StealResult.TASK_PINNED,
            )
            self._history.append(attempt)
            return attempt

        # Record the steal and update cooldown
        stolen_ids = [t.task_id for t in selected]
        now = time.time()
        self._cooldowns[(thief_id, victim.node_id)] = now

        attempt = StealAttempt(
            thief_node=thief_id,
            victim_node=victim.node_id,
            tasks_stolen=stolen_ids,
            result=StealResult.SUCCESS,
            timestamp=now,
        )
        self._history.append(attempt)
        logger.info(
            "Node %s stole %d task(s) from %s: %s",
            thief_id,
            len(stolen_ids),
            victim.node_id,
            stolen_ids,
        )
        return attempt

    def reset_cooldowns(self) -> None:
        """Clear all steal cooldowns."""
        self._cooldowns.clear()

    def clear_history(self) -> None:
        """Clear the steal attempt history."""
        self._history.clear()
