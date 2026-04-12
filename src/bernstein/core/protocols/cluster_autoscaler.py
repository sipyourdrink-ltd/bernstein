"""ENT-013: Cluster auto-scaling based on task queue depth.

Monitors the task queue depth across cluster nodes and recommends scaling
decisions (scale-up or scale-down).  Integrates with the load scaler for
system-level metrics and adds queue-depth-based decision making.

Scaling policy:
- Scale up when queue depth per node exceeds the high watermark
- Scale down when queue depth per node drops below the low watermark
- Respect cooldown periods to avoid flapping
- Enforce min/max node count boundaries

Also provides scaling backends that execute the decisions:
- KubernetesHPABackend: patches a Kubernetes HPA replica count
- NoOpBackend: logs decisions without acting (dry-run / testing)
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bernstein.core.defaults import PROTOCOL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_HIGH_WATERMARK = 10  # Tasks per node before scale-up
_DEFAULT_LOW_WATERMARK = 2  # Tasks per node before scale-down
_DEFAULT_COOLDOWN_S = PROTOCOL.cluster_autoscale_cooldown_s
_DEFAULT_MIN_NODES = PROTOCOL.cluster_min_nodes
_DEFAULT_MAX_NODES = PROTOCOL.cluster_max_nodes


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


# ---------------------------------------------------------------------------
# Scaling backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScaleResult:
    """Result of executing a scaling action.

    Attributes:
        success: Whether the scaling action succeeded.
        previous_count: Node count before the action.
        new_count: Node count after the action.
        backend: Name of the backend that executed the action.
        error: Error message if the action failed.
        timestamp: When the action was executed.
    """

    success: bool = True
    previous_count: int = 0
    new_count: int = 0
    backend: str = ""
    error: str = ""
    timestamp: float = field(default_factory=time.time)


class ScalingBackend(ABC):
    """Abstract backend that executes scaling decisions.

    Subclasses implement the actual infrastructure-level scaling (e.g.
    patching a Kubernetes HPA, calling a cloud API, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name."""

    @abstractmethod
    def current_node_count(self) -> int:
        """Return the current number of active worker nodes."""

    @abstractmethod
    def scale_to(self, target_count: int) -> ScaleResult:
        """Scale the cluster to the target node count.

        Args:
            target_count: Desired number of worker nodes.

        Returns:
            ScaleResult with the outcome.
        """


class NoOpBackend(ScalingBackend):
    """Dry-run backend that logs decisions without acting.

    Useful for testing, local development, and validating autoscaler
    logic before connecting real infrastructure.
    """

    def __init__(self, simulated_count: int = 1) -> None:
        self._count = simulated_count
        self._actions: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "noop"

    @property
    def actions(self) -> list[dict[str, Any]]:
        """Return the log of simulated scaling actions."""
        return list(self._actions)

    def current_node_count(self) -> int:
        return self._count

    def scale_to(self, target_count: int) -> ScaleResult:
        previous = self._count
        self._count = target_count
        self._actions.append(
            {"from": previous, "to": target_count, "timestamp": time.time()},
        )
        logger.info("[noop] Simulated scale %d -> %d", previous, target_count)
        return ScaleResult(
            success=True,
            previous_count=previous,
            new_count=target_count,
            backend="noop",
        )


class KubernetesHPABackend(ScalingBackend):
    """Scale cluster via Kubernetes HPA replica count.

    Uses ``kubectl`` or the Kubernetes Python client to patch the
    minReplicas/maxReplicas on a target HPA resource.  Falls back
    gracefully if the cluster is unreachable.

    Args:
        namespace: Kubernetes namespace.
        hpa_name: Name of the HorizontalPodAutoscaler resource.
        kubeconfig_path: Optional path to kubeconfig file.
    """

    def __init__(
        self,
        namespace: str = "default",
        hpa_name: str = "bernstein-workers",
        kubeconfig_path: str | None = None,
    ) -> None:
        self._namespace = namespace
        self._hpa_name = hpa_name
        self._kubeconfig_path = kubeconfig_path
        self._last_known_count: int = 0

    @property
    def name(self) -> str:
        return "kubernetes-hpa"

    def current_node_count(self) -> int:
        """Query the current HPA replica count.

        Returns 0 if the cluster is unreachable, allowing the autoscaler
        to trigger a scale-up to minimum.
        """
        import subprocess

        cmd = [
            "kubectl",
            "get",
            "hpa",
            self._hpa_name,
            "-n",
            self._namespace,
            "-o",
            "jsonpath={.status.currentReplicas}",
        ]
        if self._kubeconfig_path:
            cmd.extend(["--kubeconfig", self._kubeconfig_path])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                count = int(result.stdout.strip())
                self._last_known_count = count
                return count
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            logger.warning("Failed to query HPA replicas: %s", exc)
        return self._last_known_count

    def scale_to(self, target_count: int) -> ScaleResult:
        """Patch the HPA minReplicas to the target count.

        Args:
            target_count: Desired replica count.

        Returns:
            ScaleResult with outcome.
        """
        import subprocess

        previous = self.current_node_count()
        patch = f'{{"spec":{{"minReplicas":{target_count}}}}}'
        cmd = [
            "kubectl",
            "patch",
            "hpa",
            self._hpa_name,
            "-n",
            self._namespace,
            "--type=merge",
            "-p",
            patch,
        ]
        if self._kubeconfig_path:
            cmd.extend(["--kubeconfig", self._kubeconfig_path])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                logger.info(
                    "Scaled HPA %s/%s: %d -> %d",
                    self._namespace,
                    self._hpa_name,
                    previous,
                    target_count,
                )
                self._last_known_count = target_count
                return ScaleResult(
                    success=True,
                    previous_count=previous,
                    new_count=target_count,
                    backend="kubernetes-hpa",
                )
            error_msg = result.stderr.strip() or result.stdout.strip()
            logger.error("kubectl patch failed: %s", error_msg)
            return ScaleResult(
                success=False,
                previous_count=previous,
                new_count=previous,
                backend="kubernetes-hpa",
                error=error_msg,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error("kubectl patch error: %s", exc)
            return ScaleResult(
                success=False,
                previous_count=previous,
                new_count=previous,
                backend="kubernetes-hpa",
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Autoscale executor — ties decisions to backends
# ---------------------------------------------------------------------------


class AutoscaleExecutor:
    """Combines the autoscaler engine with a scaling backend.

    Evaluates a queue snapshot, and if the decision requires a change,
    executes it through the configured backend.

    Args:
        autoscaler: The decision engine.
        backend: The infrastructure backend.
    """

    def __init__(
        self,
        autoscaler: ClusterAutoscaler,
        backend: ScalingBackend,
    ) -> None:
        self._autoscaler = autoscaler
        self._backend = backend
        self._results: list[ScaleResult] = []

    @property
    def autoscaler(self) -> ClusterAutoscaler:
        """Return the autoscaler engine."""
        return self._autoscaler

    @property
    def backend(self) -> ScalingBackend:
        """Return the scaling backend."""
        return self._backend

    @property
    def results(self) -> list[ScaleResult]:
        """Return past execution results."""
        return list(self._results)

    def tick(self, snapshot: QueueSnapshot) -> tuple[ScaleDecision, ScaleResult | None]:
        """Run one evaluation-and-execute cycle.

        Args:
            snapshot: Current queue state.

        Returns:
            Tuple of (decision, result).  ``result`` is None when no
            scaling action was needed.
        """
        decision = self._autoscaler.evaluate(snapshot)
        if decision.direction == ScaleDirection.NONE:
            return decision, None

        result = self._backend.scale_to(decision.recommended_nodes)
        self._results.append(result)
        return decision, result
