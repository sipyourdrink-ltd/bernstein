"""Auto-adjust max concurrent agents based on system load."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Thresholds for CPU-based adjustment
CPU_HIGH_THRESHOLD = 80.0  # Reduce agents if CPU > 80%
CPU_LOW_THRESHOLD = 30.0  # Increase agents if CPU < 30%
MEMORY_HIGH_THRESHOLD = 80.0  # Reduce agents if memory > 80%
MEMORY_LOW_THRESHOLD = 30.0  # Increase agents if memory < 30%

# Adjustment parameters
ADJUSTMENT_COOLDOWN_SECONDS = 60  # Minimum time between adjustments
MAX_ADJUSTMENT_STEP = 2  # Maximum agents to add/remove in one adjustment
MIN_AGENTS = 1
DEFAULT_POLL_INTERVAL_SECONDS = 30


@dataclass
class LoadAdjustmentResult:
    """Result of a load-based adjustment decision."""

    current_cpu: float
    current_memory: float
    current_agents: int
    recommended_agents: int
    adjustment: int  # Positive = increase, negative = decrease
    reason: str
    timestamp: float


class LoadBasedAgentScaler:
    """Automatically adjust max concurrent agents based on system load.

    Monitors CPU and memory usage and adjusts the maximum number of
    concurrent agents to optimize resource utilization.

    Args:
        min_agents: Minimum number of agents to maintain.
        max_agents: Maximum number of agents allowed.
        poll_interval: Seconds between load checks.
    """

    def __init__(
        self,
        min_agents: int = MIN_AGENTS,
        max_agents: int = 10,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._min_agents = min_agents
        self._max_agents = max_agents
        self._poll_interval = poll_interval
        self._last_adjustment: float = 0.0
        self._current_max: int = max_agents
        self._history: list[LoadAdjustmentResult] = []

    def get_system_load(self) -> tuple[float, float]:
        """Get current CPU and memory usage percentages.

        Returns:
            Tuple of (cpu_percent, memory_percent).
        """
        try:
            # Use os.getloadavg() on Unix or fallback
            if hasattr(os, "getloadavg"):
                load_avg = os.getloadavg()
                # Normalize to percentage (assume 2 cores as baseline)
                cpu_percent = min(100.0, (load_avg[0] / 2.0) * 100)
            else:
                cpu_percent = 50.0  # Default if unavailable

            # Memory usage
            try:
                import resource

                rusage = resource.getrusage(resource.RUSAGE_SELF)
                # Max RSS in KB, estimate percentage (assume 8GB baseline)
                max_mem_kb = rusage.ru_maxrss
                memory_percent = min(100.0, (max_mem_kb / 8_000_000) * 100)
            except Exception:
                memory_percent = 50.0  # Default if unavailable

            return cpu_percent, memory_percent
        except Exception as exc:
            logger.debug("Failed to get system load: %s", exc)
            return 50.0, 50.0

    def should_adjust(self) -> bool:
        """Check if enough time has passed since last adjustment.

        Returns:
            True if adjustment is allowed.
        """
        return (time.time() - self._last_adjustment) >= self._poll_interval

    def calculate_recommendation(
        self,
        current_agents: int,
    ) -> LoadAdjustmentResult:
        """Calculate recommended agent count based on current load.

        Args:
            current_agents: Current number of active agents.

        Returns:
            LoadAdjustmentResult with recommendation.
        """
        cpu, memory = self.get_system_load()

        # Determine adjustment direction
        adjustment = 0
        reason = "Load within normal range"

        if cpu > CPU_HIGH_THRESHOLD or memory > MEMORY_HIGH_THRESHOLD:
            # High load - reduce agents
            adjustment = -min(MAX_ADJUSTMENT_STEP, current_agents - self._min_agents)
            if cpu > CPU_HIGH_THRESHOLD:
                reason = f"High CPU usage ({cpu:.1f}%)"
            else:
                reason = f"High memory usage ({memory:.1f}%)"
        elif cpu < CPU_LOW_THRESHOLD and memory < MEMORY_LOW_THRESHOLD:
            # Low load - can increase agents
            adjustment = min(MAX_ADJUSTMENT_STEP, self._max_agents - current_agents)
            reason = f"Low resource usage (CPU: {cpu:.1f}%, Memory: {memory:.1f}%)"

        recommended = max(self._min_agents, min(self._max_agents, current_agents + adjustment))

        result = LoadAdjustmentResult(
            current_cpu=round(cpu, 1),
            current_memory=round(memory, 1),
            current_agents=current_agents,
            recommended_agents=recommended,
            adjustment=adjustment,
            reason=reason,
            timestamp=time.time(),
        )

        self._history.append(result)
        return result

    def apply_adjustment(
        self,
        current_agents: int,
        force: bool = False,
    ) -> LoadAdjustmentResult | None:
        """Apply load-based adjustment if conditions are met.

        Args:
            current_agents: Current number of active agents.
            force: Force adjustment even if cooldown not elapsed.

        Returns:
            LoadAdjustmentResult if adjustment was made, None otherwise.
        """
        if not force and not self.should_adjust():
            return None

        result = self.calculate_recommendation(current_agents)

        if result.adjustment != 0:
            self._current_max = result.recommended_agents
            self._last_adjustment = time.time()
            logger.info(
                "Auto-adjusted max agents: %d → %d (%s)",
                current_agents,
                result.recommended_agents,
                result.reason,
            )
            return result

        return None

    def get_current_max(self) -> int:
        """Get current maximum agent limit.

        Returns:
            Current max agents setting.
        """
        return self._current_max

    def set_limits(self, min_agents: int, max_agents: int) -> None:
        """Update agent limits.

        Args:
            min_agents: New minimum agents.
            max_agents: New maximum agents.
        """
        self._min_agents = min_agents
        self._max_agents = max_agents
        self._current_max = min(max(self._current_max, min_agents), max_agents)

    def get_history(self) -> list[LoadAdjustmentResult]:
        """Get adjustment history.

        Returns:
            List of LoadAdjustmentResult instances.
        """
        return list(self._history)

    def get_summary(self) -> dict[str, Any]:
        """Get scaler summary.

        Returns:
            Summary dictionary.
        """
        return {
            "current_max": self._current_max,
            "min_agents": self._min_agents,
            "max_agents": self._max_agents,
            "last_adjustment": self._last_adjustment,
            "adjustment_count": len(self._history),
            "poll_interval": self._poll_interval,
        }
