"""SEC-016: Differential privacy for telemetry exports.

Adds noise to aggregated metrics to prevent individual task inference.
Wraps the existing differential_privacy module with telemetry-specific
logic for privacy budget tracking and field-level sensitivity.

Usage::

    from bernstein.core.dp_telemetry import (
        DPTelemetryExporter,
        TelemetryPrivacyConfig,
        PrivacyBudgetTracker,
    )

    config = TelemetryPrivacyConfig(epsilon=1.0, delta=1e-5)
    exporter = DPTelemetryExporter(config)
    safe_data = exporter.export(raw_metrics)
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast

from bernstein.core.differential_privacy import DPConfig, GaussianMechanism

logger = logging.getLogger(__name__)


@dataclass
class TelemetryPrivacyConfig:
    """Configuration for differential privacy in telemetry exports.

    Attributes:
        epsilon: Total privacy budget.  Smaller = more privacy.
        delta: Failure probability (must be < 1).
        max_queries: Maximum number of queries before budget exhaustion.
        clip_min: Lower bound for noisy values.
        field_sensitivities: Per-field L2 sensitivity overrides.
    """

    epsilon: float = 1.0
    delta: float = 1e-5
    max_queries: int = 1000
    clip_min: float = 0.0
    field_sensitivities: dict[str, float] = field(default_factory=dict[str, float])


@dataclass
class BudgetEntry:
    """A single privacy budget expenditure.

    Attributes:
        query_id: Identifier for the query.
        epsilon_spent: Epsilon consumed by this query.
        timestamp: When the query was executed.
        field_count: Number of fields perturbed.
    """

    query_id: str
    epsilon_spent: float
    timestamp: float
    field_count: int


class PrivacyBudgetTracker:
    """Tracks cumulative privacy budget consumption.

    Ensures that the total epsilon spent does not exceed the configured
    budget.  Queries that would exceed the budget are rejected.

    Args:
        total_epsilon: Total privacy budget.
        max_queries: Maximum queries allowed.
    """

    def __init__(self, total_epsilon: float, max_queries: int = 1000) -> None:
        self._total_epsilon = total_epsilon
        self._max_queries = max_queries
        self._entries: list[BudgetEntry] = []
        self._epsilon_spent: float = 0.0

    @property
    def total_epsilon(self) -> float:
        """Return the total privacy budget."""
        return self._total_epsilon

    @property
    def epsilon_spent(self) -> float:
        """Return the total epsilon consumed so far."""
        return self._epsilon_spent

    @property
    def epsilon_remaining(self) -> float:
        """Return the remaining privacy budget."""
        return max(0.0, self._total_epsilon - self._epsilon_spent)

    @property
    def queries_remaining(self) -> int:
        """Return the number of queries remaining."""
        return max(0, self._max_queries - len(self._entries))

    @property
    def entries(self) -> list[BudgetEntry]:
        """Return the budget expenditure log."""
        return list(self._entries)

    def can_spend(self, epsilon: float) -> bool:
        """Check if spending epsilon would stay within budget.

        Args:
            epsilon: Epsilon to spend.

        Returns:
            True if the budget allows the expenditure.
        """
        if len(self._entries) >= self._max_queries:
            return False
        return (self._epsilon_spent + epsilon) <= self._total_epsilon

    def spend(self, query_id: str, epsilon: float, field_count: int) -> bool:
        """Record a budget expenditure.

        Args:
            query_id: Identifier for the query.
            epsilon: Epsilon consumed.
            field_count: Number of fields perturbed.

        Returns:
            True if the expenditure was recorded, False if budget exceeded.
        """
        if not self.can_spend(epsilon):
            logger.warning(
                "Privacy budget exhausted: spent=%.4f, requested=%.4f, total=%.4f",
                self._epsilon_spent,
                epsilon,
                self._total_epsilon,
            )
            return False

        entry = BudgetEntry(
            query_id=query_id,
            epsilon_spent=epsilon,
            timestamp=time.time(),
            field_count=field_count,
        )
        self._entries.append(entry)
        self._epsilon_spent += epsilon
        return True

    def reset(self) -> None:
        """Reset the budget tracker."""
        self._entries.clear()
        self._epsilon_spent = 0.0


# ---------------------------------------------------------------------------
# Default field sensitivities for telemetry metrics
# ---------------------------------------------------------------------------

_DEFAULT_SENSITIVITIES: dict[str, float] = {
    "task_count": 50.0,
    "agent_count": 10.0,
    "duration_seconds": 3600.0,
    "tokens_used": 100_000.0,
    "cost_usd": 5.0,
    "success_rate": 1.0,
    "error_count": 50.0,
    "avg_latency_ms": 10_000.0,
    "total_cost_usd": 50.0,
    "tasks_completed": 50.0,
    "tasks_failed": 50.0,
}

# Fields that must never be perturbed
_PASSTHROUGH_FIELDS: frozenset[str] = frozenset(
    {
        "agent_id",
        "task_id",
        "role",
        "model",
        "provider",
        "status",
        "timestamp",
        "session_id",
        "version",
    }
)


class DPTelemetryExporter:
    """Exports telemetry data with differential privacy guarantees.

    Applies calibrated Gaussian noise to numeric fields before export.
    Tracks privacy budget to prevent over-querying.

    Args:
        config: Privacy configuration.
    """

    def __init__(self, config: TelemetryPrivacyConfig) -> None:
        self._config = config
        self._dp_config = DPConfig(
            epsilon=config.epsilon,
            delta=config.delta,
            clip_min=config.clip_min,
        )
        self._budget = PrivacyBudgetTracker(
            total_epsilon=config.epsilon,
            max_queries=config.max_queries,
        )
        self._sensitivities = {**_DEFAULT_SENSITIVITIES, **config.field_sensitivities}
        self._export_count = 0

    @property
    def budget(self) -> PrivacyBudgetTracker:
        """Return the privacy budget tracker."""
        return self._budget

    def _perturb_value(self, value: Any, field_name: str) -> Any:
        """Apply DP noise to a single value if it is numeric.

        Args:
            value: The value to perturb.
            field_name: Name of the field (for sensitivity lookup).

        Returns:
            Perturbed value if numeric, original value otherwise.
        """
        if value is None or not isinstance(value, (int, float)):
            return value

        sensitivity = self._sensitivities.get(field_name, 1.0)
        mechanism = GaussianMechanism(sensitivity=sensitivity, config=self._dp_config)
        return mechanism.add_noise(float(value))

    def _perturb_dict(self, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Apply DP noise to all numeric fields in a dict.

        Args:
            data: The dict to perturb.

        Returns:
            Tuple of (perturbed dict, number of fields perturbed).
        """
        result: dict[str, Any] = {}
        count = 0
        for key, value in data.items():
            if key in _PASSTHROUGH_FIELDS:
                result[key] = value
            elif isinstance(value, (int, float)):
                result[key] = self._perturb_value(value, key)
                count += 1
            elif isinstance(value, dict):
                inner, inner_count = self._perturb_dict(cast("dict[str, Any]", value))
                result[key] = inner
                count += inner_count
            elif isinstance(value, list):
                perturbed_list: list[Any] = []
                for item in cast("list[Any]", value):
                    if isinstance(item, dict):
                        inner, inner_count = self._perturb_dict(cast("dict[str, Any]", item))
                        perturbed_list.append(inner)
                        count += inner_count
                    else:
                        perturbed_list.append(item)
                result[key] = perturbed_list
            else:
                result[key] = value
        return result, count

    def export(self, raw_metrics: dict[str, Any]) -> dict[str, Any] | None:
        """Export metrics with differential privacy noise applied.

        Returns None if the privacy budget is exhausted.

        Args:
            raw_metrics: Raw telemetry data to export.

        Returns:
            Privacy-safe copy of the metrics, or None if budget exhausted.
        """
        data = copy.deepcopy(raw_metrics)
        self._export_count += 1
        query_id = f"export-{self._export_count}"

        # Per-query epsilon = total / max_queries (basic composition)
        per_query_epsilon = self._config.epsilon / max(1, self._config.max_queries)

        result, field_count = self._perturb_dict(data)

        if not self._budget.spend(query_id, per_query_epsilon, field_count):
            logger.warning("Telemetry export rejected: privacy budget exhausted")
            return None

        logger.debug(
            "Exported telemetry with DP: query=%s fields_perturbed=%d epsilon_spent=%.6f",
            query_id,
            field_count,
            per_query_epsilon,
        )

        return result
