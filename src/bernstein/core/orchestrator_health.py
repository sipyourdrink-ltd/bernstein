"""Orchestrator health score: composite 0-100 score from subsystem checks.

Combines heartbeat status, circuit breaker state, memory guard level,
WAL health, and server connectivity into a single score exposed on
``/health/score`` and the TUI dashboard.

Usage::

    scorer = OrchestratorHealthScorer()
    result = scorer.evaluate(
        heartbeat_ok=True,
        circuit_breaker_open=False,
        memory_used_pct=65.0,
        wal_healthy=True,
        server_reachable=True,
    )
    print(result.score)  # 0-100
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class HealthGrade(StrEnum):
    """Human-readable health grade."""

    HEALTHY = "healthy"  # 80-100
    DEGRADED = "degraded"  # 50-79
    UNHEALTHY = "unhealthy"  # 20-49
    CRITICAL = "critical"  # 0-19


@dataclass(frozen=True)
class ComponentHealth:
    """Health status of a single orchestrator component.

    Attributes:
        name: Component name (e.g. ``"heartbeat"``, ``"wal"``).
        healthy: Whether this component is functioning normally.
        score: Component score contribution (0-100).
        weight: Relative weight in the composite score.
        detail: Optional human-readable detail.
    """

    name: str
    healthy: bool
    score: int
    weight: float
    detail: str = ""


@dataclass(frozen=True)
class HealthScoreResult:
    """Composite orchestrator health score.

    Attributes:
        score: Overall score (0-100).
        grade: Human-readable health grade.
        components: Per-component breakdown.
        message: One-line summary.
    """

    score: int
    grade: HealthGrade
    components: list[ComponentHealth]
    message: str

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict.

        Returns:
            Dictionary with score, grade, components, and message.
        """
        return {
            "score": self.score,
            "grade": self.grade.value,
            "message": self.message,
            "components": [
                {
                    "name": c.name,
                    "healthy": c.healthy,
                    "score": c.score,
                    "weight": c.weight,
                    "detail": c.detail,
                }
                for c in self.components
            ],
        }


@dataclass
class OrchestratorHealthScorer:
    """Computes a composite health score from subsystem checks.

    Default weights reflect the relative importance of each subsystem
    to overall orchestrator reliability.

    Args:
        heartbeat_weight: Weight for heartbeat health (0.0-1.0).
        circuit_breaker_weight: Weight for circuit breaker state.
        memory_weight: Weight for memory guard health.
        wal_weight: Weight for WAL integrity.
        server_weight: Weight for server connectivity.
    """

    heartbeat_weight: float = 0.25
    circuit_breaker_weight: float = 0.20
    memory_weight: float = 0.20
    wal_weight: float = 0.15
    server_weight: float = 0.20

    def evaluate(
        self,
        *,
        heartbeat_ok: bool = True,
        circuit_breaker_open: bool = False,
        memory_used_pct: float = 0.0,
        wal_healthy: bool = True,
        server_reachable: bool = True,
        consecutive_server_failures: int = 0,
    ) -> HealthScoreResult:
        """Compute the composite health score.

        Args:
            heartbeat_ok: Whether agent heartbeats are current.
            circuit_breaker_open: Whether any circuit breaker is tripped.
            memory_used_pct: System memory utilization (0.0-100.0).
            wal_healthy: Whether the WAL is writable and uncorrupted.
            server_reachable: Whether the task server responded to the
                last health check.
            consecutive_server_failures: Number of consecutive failed
                server health checks.

        Returns:
            Composite health score with component breakdown.
        """
        components: list[ComponentHealth] = []

        # Heartbeat
        hb_score = 100 if heartbeat_ok else 0
        components.append(
            ComponentHealth(
                name="heartbeat",
                healthy=heartbeat_ok,
                score=hb_score,
                weight=self.heartbeat_weight,
                detail="all agents reporting" if heartbeat_ok else "stale heartbeats detected",
            )
        )

        # Circuit breaker
        cb_score = 0 if circuit_breaker_open else 100
        components.append(
            ComponentHealth(
                name="circuit_breaker",
                healthy=not circuit_breaker_open,
                score=cb_score,
                weight=self.circuit_breaker_weight,
                detail="circuit breaker OPEN" if circuit_breaker_open else "closed",
            )
        )

        # Memory guard
        mem_score = _memory_score(memory_used_pct)
        mem_healthy = memory_used_pct < 80.0
        components.append(
            ComponentHealth(
                name="memory",
                healthy=mem_healthy,
                score=mem_score,
                weight=self.memory_weight,
                detail=f"{memory_used_pct:.1f}% used",
            )
        )

        # WAL
        wal_score = 100 if wal_healthy else 0
        components.append(
            ComponentHealth(
                name="wal",
                healthy=wal_healthy,
                score=wal_score,
                weight=self.wal_weight,
                detail="WAL writable" if wal_healthy else "WAL error",
            )
        )

        # Server connectivity
        srv_score = _server_score(server_reachable, consecutive_server_failures)
        components.append(
            ComponentHealth(
                name="server",
                healthy=server_reachable,
                score=srv_score,
                weight=self.server_weight,
                detail=(
                    "server reachable" if server_reachable else f"unreachable ({consecutive_server_failures} failures)"
                ),
            )
        )

        # Weighted composite
        total_weight = sum(c.weight for c in components)
        if total_weight <= 0:
            total_weight = 1.0
        raw_score = sum(c.score * c.weight for c in components) / total_weight
        score = max(0, min(100, round(raw_score)))
        grade = _classify_grade(score)

        unhealthy_names = [c.name for c in components if not c.healthy]
        message = f"Degraded: {', '.join(unhealthy_names)} unhealthy" if unhealthy_names else "All systems operational"

        return HealthScoreResult(
            score=score,
            grade=grade,
            components=components,
            message=message,
        )


def _memory_score(used_pct: float) -> int:
    """Convert memory utilization to a 0-100 health score.

    Args:
        used_pct: Memory utilization percentage.

    Returns:
        Health score (100 when low usage, 0 at 95%+).
    """
    if used_pct < 60.0:
        return 100
    if used_pct < 80.0:
        # Linear degradation 100 -> 60 between 60% and 80%
        return int(100 - (used_pct - 60.0) * 2)
    if used_pct < 90.0:
        # Steeper degradation 60 -> 20 between 80% and 90%
        return int(60 - (used_pct - 80.0) * 4)
    if used_pct < 95.0:
        # Critical zone: 20 -> 5
        return int(20 - (used_pct - 90.0) * 3)
    return 0


def _server_score(reachable: bool, consecutive_failures: int) -> int:
    """Score server health based on reachability and failure streak.

    Args:
        reachable: Whether the last health check succeeded.
        consecutive_failures: Number of consecutive failures.

    Returns:
        Health score (0-100).
    """
    if reachable:
        return 100
    # Degrade based on consecutive failures
    return max(0, 80 - consecutive_failures * 20)


def _classify_grade(score: int) -> HealthGrade:
    """Map a numeric score to a health grade.

    Args:
        score: Numeric health score (0-100).

    Returns:
        Corresponding health grade.
    """
    if score >= 80:
        return HealthGrade.HEALTHY
    if score >= 50:
        return HealthGrade.DEGRADED
    if score >= 20:
        return HealthGrade.UNHEALTHY
    return HealthGrade.CRITICAL
