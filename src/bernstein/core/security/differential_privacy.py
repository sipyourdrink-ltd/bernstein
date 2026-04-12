"""(ε, δ)-Differential privacy mechanisms for telemetry export.

Adds calibrated Gaussian noise to numeric telemetry fields before any export
operation, preventing re-identification of developers or proprietary usage
patterns from aggregate metrics.

Usage::

    from bernstein.core.differential_privacy import DPConfig, apply_dp_to_export

    cfg = DPConfig(epsilon=1.0, delta=1e-5)
    safe_data = apply_dp_to_export(raw_export_dict, cfg)
    json.dump(safe_data, f)
"""

from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass
from typing import Any


@dataclass
class DPConfig:
    """Configuration for (ε, δ)-differential privacy via the Gaussian mechanism.

    Args:
        epsilon: Privacy budget.  Smaller = more privacy, more noise.
        delta: Failure probability of the privacy guarantee (must be < 1).
        clip_min: Lower bound applied after noise addition.  Defaults to 0 so
            counts and costs are never negative.
    """

    epsilon: float = 1.0
    delta: float = 1e-5
    clip_min: float = 0.0


class GaussianMechanism:
    """Gaussian mechanism for (ε, δ)-DP.

    Adds noise drawn from N(0, sigma^2) where::

        sigma = sensitivity * sqrt(2 * ln(1.25 / delta)) / epsilon

    This satisfies (epsilon, delta)-DP for a query with the given L2 sensitivity.

    Args:
        sensitivity: Global L2 sensitivity of the numeric field.
        config: Privacy parameters.
    """

    def __init__(self, sensitivity: float, config: DPConfig) -> None:
        self._sensitivity = sensitivity
        self._config = config
        self._sigma = self._compute_sigma()

    def _compute_sigma(self) -> float:
        return self._sensitivity * math.sqrt(2 * math.log(1.25 / self._config.delta)) / self._config.epsilon

    @property
    def sigma(self) -> float:
        """Standard deviation of the added Gaussian noise."""
        return self._sigma

    def add_noise(self, value: float) -> float:
        """Return *value* plus a Gaussian noise sample, clamped to clip_min.

        Args:
            value: Original numeric measurement.

        Returns:
            Privatised value >= config.clip_min.
        """
        noisy = value + random.gauss(0.0, self._sigma)
        return max(self._config.clip_min, noisy)


# ---------------------------------------------------------------------------
# Per-field sensitivity defaults
# Field sensitivities are chosen conservatively: upper bound on how much one
# developer's activity could shift the aggregate metric in isolation.
# ---------------------------------------------------------------------------

_TASK_SENSITIVITIES: dict[str, float] = {
    "duration_seconds": 3600.0,  # one task could take up to an hour
    "tokens_used": 100_000.0,  # generous upper bound per task
    "cost_usd": 5.0,  # max cost per single task
}

_AGENT_SENSITIVITIES: dict[str, float] = {
    "tasks_completed": 50.0,
    "tasks_failed": 50.0,
    "total_tokens": 1_000_000.0,
    "total_cost_usd": 50.0,
}

_SUMMARY_SENSITIVITIES: dict[str, float] = {
    "total_tasks": 50.0,
    "successful_tasks": 50.0,
    "failed_tasks": 50.0,
    "success_rate": 1.0,
    "janitor_pass_rate": 1.0,
    "total_agents": 10.0,
    "total_cost_usd": 50.0,
    "avg_completion_time_seconds": 3600.0,
}

# Fields that must never be perturbed (categorical / identifiers / booleans / None-able strings)
_TASK_PASSTHROUGH: frozenset[str] = frozenset({"task_id", "role", "model", "provider", "success", "error"})
_AGENT_PASSTHROUGH: frozenset[str] = frozenset({"agent_id", "role"})
_SUMMARY_PASSTHROUGH: frozenset[str] = frozenset({"provider_stats", "provider_health", "quota_status"})


def _perturb(value: Any, sensitivity: float, config: DPConfig) -> Any:
    """Apply DP noise to *value* if it is a non-None number, else return as-is."""
    if value is None or not isinstance(value, (int, float)):
        return value
    mech = GaussianMechanism(sensitivity=sensitivity, config=config)
    return mech.add_noise(float(value))


def apply_dp_to_export(data: dict[str, Any], config: DPConfig) -> dict[str, Any]:
    """Return a deep copy of *data* with numeric telemetry fields privatised.

    Applies the Gaussian mechanism to numeric fields in ``task_metrics``,
    ``agent_metrics``, and the top-level ``summary`` dict.  Categorical
    identifiers, booleans, ``None`` values, and timestamps are left unchanged.

    Args:
        data: Raw export dict as produced by :func:`metric_export.export_metrics`.
        config: Privacy parameters.

    Returns:
        New dict — original *data* is never mutated.
    """
    result: dict[str, Any] = copy.deepcopy(data)

    # -- task_metrics ---------------------------------------------------------
    for task in result.get("task_metrics", []):
        for field_name, sensitivity in _TASK_SENSITIVITIES.items():
            if field_name in task and field_name not in _TASK_PASSTHROUGH:
                task[field_name] = _perturb(task[field_name], sensitivity, config)

    # -- agent_metrics --------------------------------------------------------
    for agent in result.get("agent_metrics", []):
        for field_name, sensitivity in _AGENT_SENSITIVITIES.items():
            if field_name in agent and field_name not in _AGENT_PASSTHROUGH:
                agent[field_name] = _perturb(agent[field_name], sensitivity, config)

    # -- summary --------------------------------------------------------------
    summary = result.get("summary", {})
    for field_name, sensitivity in _SUMMARY_SENSITIVITIES.items():
        if field_name in summary and field_name not in _SUMMARY_PASSTHROUGH:
            summary[field_name] = _perturb(summary[field_name], sensitivity, config)

    return result
