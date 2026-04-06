"""ENT-013: Cluster auto-scaling based on task queue depth.

Monitors the task queue depth across cluster nodes and recommends scaling
decisions (scale-up or scale-down).  Integrates with the load scaler for
system-level metrics and adds queue-depth-based decision making.

Scaling policy:
- Scale up when queue depth per node exceeds the high watermark
- Scale down when queue depth per node drops below the low watermark
- Respect cooldown periods to avoid flapping
- Enforce min/max node count boundaries
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

_DEFAULT_HIGH_WATERMARK = 10  # Tasks per node before scale-up
_DEFAULT_LOW_WATERMARK = 2  # Tasks per node before scale-down
_DEFAULT_COOLDOWN_S = 120.0  # Min seconds between scaling actions
_DEFAULT_MIN_NODES = 1
_DEFAULT_MAX_NODES = 20


class ScaleDirection(StrEnum):
    """Direction of a scaling action."""

    UP = "up"
    DOWN = "down"
    NONE = "none"


@dataclass(frozen=True)
class AutoscaleConfig:
    """Cluster autoscaler configuration.

    Attributes:
        enabled: Whether autoscaling is active.
        high_watermark: Queue depth per node triggering scale-up.
        low_watermark: Queue depth per node triggering scale-down.
        cooldown_s: Minimum seconds between scaling actions.
        min_nodes: Minimum number of nodes to maintain.
        max_nodes: Maximum number of nodes allowed.
        scale_up_step: Number of nodes to add per scale-up.
        scale_down_step: Number of nodes to remove per scale-down.
    """

    enabled: bool = True
    high_watermark: int = _DEFAULT_HIGH_WATERMARK
    low_watermark: int = _DEFAULT_LOW_WATERMARK
    cooldown_s: float = _DEFAULT_COOLDOWN_S
    min_nodes: int = _DEFAULT_MIN_NODES
    max_nodes: int = _DEFAULT_MAX_NODES
    scale_up_step: int = 1
    scale_down_step: int = 1


# ---------------------------------------------------------------------------
# Scaling decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScaleDecision:
    """Represents a scaling recommendation.

    Attributes:
        direction: Scale up, down, or hold.
        current_nodes: Current number of active nodes.
        recommended_nodes: Recommended number of nodes.
        queue_depth: Current total queue depth.
        queue_per_node: Average queue depth per node.
        reason: Human-readable explanation.
        timestamp: When the decision was made.
    """

    direction: ScaleDirection = ScaleDirection.NONE
    current_nodes: int = 0
    recommended_nodes: int = 0
    queue_depth: int = 0
    queue_per_node: float = 0.0
    reason: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Queue snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueSnapshot:
    """Snapshot of cluster queue state.

    Attributes:
        total_queued: Total tasks queued across all nodes.
        total_running: Total tasks currently running.
        node_count: Number of active nodes.
        node_queues: Per-node queue depths.
        timestamp: When the snapshot was taken.
    """

    total_queued: int = 0
    total_running: int = 0
    node_count: int = 0
    node_queues: dict[str, int] = field(default_factory=dict[str, int])
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Autoscaler engine
# ---------------------------------------------------------------------------


class ClusterAutoscaler:
    """Cluster autoscaler based on task queue depth.

    Evaluates queue depth metrics and produces scaling decisions respecting
    cooldown periods and node count boundaries.

    Args:
        config: Autoscaler configuration.
    """

    def __init__(self, config: AutoscaleConfig | None = None) -> None:
        self._config = config or AutoscaleConfig()
        self._last_scale_time: float = 0.0
        self._history: list[ScaleDecision] = []

    @property
    def config(self) -> AutoscaleConfig:
        """Return the autoscale configuration."""
        return self._config

    @property
    def history(self) -> list[ScaleDecision]:
        """Return the scaling decision history."""
        return list(self._history)

    def _in_cooldown(self) -> bool:
        """Check if we are still within a scaling cooldown period."""
        return (time.time() - self._last_scale_time) < self._config.cooldown_s

    def evaluate(self, snapshot: QueueSnapshot) -> ScaleDecision:
        """Evaluate the queue state and recommend a scaling action.

        Args:
            snapshot: Current cluster queue snapshot.

        Returns:
            ScaleDecision with the recommendation.
        """
        if not self._config.enabled:
            decision = ScaleDecision(
                direction=ScaleDirection.NONE,
                current_nodes=snapshot.node_count,
                recommended_nodes=snapshot.node_count,
                queue_depth=snapshot.total_queued,
                reason="Autoscaling disabled",
            )
            self._history.append(decision)
            return decision

        if snapshot.node_count == 0:
            # No nodes at all — must scale up to minimum
            decision = ScaleDecision(
                direction=ScaleDirection.UP,
                current_nodes=0,
                recommended_nodes=self._config.min_nodes,
                queue_depth=snapshot.total_queued,
                queue_per_node=0.0,
                reason="No nodes available, scaling to minimum",
            )
            self._history.append(decision)
            self._last_scale_time = time.time()
            return decision

        queue_per_node = snapshot.total_queued / snapshot.node_count

        # Check cooldown
        if self._in_cooldown():
            decision = ScaleDecision(
                direction=ScaleDirection.NONE,
                current_nodes=snapshot.node_count,
                recommended_nodes=snapshot.node_count,
                queue_depth=snapshot.total_queued,
                queue_per_node=queue_per_node,
                reason="In cooldown period",
            )
            self._history.append(decision)
            return decision

        # Scale up
        if queue_per_node > self._config.high_watermark:
            new_count = min(
                snapshot.node_count + self._config.scale_up_step,
                self._config.max_nodes,
            )
            if new_count > snapshot.node_count:
                decision = ScaleDecision(
                    direction=ScaleDirection.UP,
                    current_nodes=snapshot.node_count,
                    recommended_nodes=new_count,
                    queue_depth=snapshot.total_queued,
                    queue_per_node=queue_per_node,
                    reason=(
                        f"Queue depth {queue_per_node:.1f}/node exceeds high watermark {self._config.high_watermark}"
                    ),
                )
                self._history.append(decision)
                self._last_scale_time = time.time()
                logger.info(
                    "Scale UP: %d -> %d nodes (queue/node=%.1f)",
                    snapshot.node_count,
                    new_count,
                    queue_per_node,
                )
                return decision

        # Scale down
        if queue_per_node < self._config.low_watermark:
            new_count = max(
                snapshot.node_count - self._config.scale_down_step,
                self._config.min_nodes,
            )
            if new_count < snapshot.node_count:
                decision = ScaleDecision(
                    direction=ScaleDirection.DOWN,
                    current_nodes=snapshot.node_count,
                    recommended_nodes=new_count,
                    queue_depth=snapshot.total_queued,
                    queue_per_node=queue_per_node,
                    reason=(f"Queue depth {queue_per_node:.1f}/node below low watermark {self._config.low_watermark}"),
                )
                self._history.append(decision)
                self._last_scale_time = time.time()
                logger.info(
                    "Scale DOWN: %d -> %d nodes (queue/node=%.1f)",
                    snapshot.node_count,
                    new_count,
                    queue_per_node,
                )
                return decision

        # No action needed
        decision = ScaleDecision(
            direction=ScaleDirection.NONE,
            current_nodes=snapshot.node_count,
            recommended_nodes=snapshot.node_count,
            queue_depth=snapshot.total_queued,
            queue_per_node=queue_per_node,
            reason="Queue depth within normal range",
        )
        self._history.append(decision)
        return decision

    def clear_history(self) -> None:
        """Clear the scaling decision history."""
        self._history.clear()

    def reset_cooldown(self) -> None:
        """Reset the cooldown timer."""
        self._last_scale_time = 0.0
