"""Agent-specific operational metrics.

Tracks decision latency, retry causes, and compound success rates
that standard application metrics don't capture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

RetryCategory = Literal[
    "quality_gate_fail",
    "timeout",
    "crash",
    "model_error",
    "rate_limit",
    "budget_exceeded",
    "unknown",
]


@dataclass
class AgentMetrics:
    """Collected metrics for a single agent execution."""

    task_id: str
    agent_id: str
    model: str
    role: str
    complexity: str
    claim_time: float = 0.0
    first_output_time: float = 0.0
    completion_time: float = 0.0
    step_count: int = 0
    result: Literal["pass", "fail", "retry"] = "pass"
    retry_category: RetryCategory | None = None
    cost_usd: float = 0.0

    @property
    def decision_latency_s(self) -> float:
        """Time from task claim to first agent output."""
        if self.first_output_time and self.claim_time:
            return self.first_output_time - self.claim_time
        return 0.0

    @property
    def execution_duration_s(self) -> float:
        """Total execution time."""
        if self.completion_time and self.claim_time:
            return self.completion_time - self.claim_time
        return 0.0


@dataclass
class AgentMetricsCollector:
    """Collects and aggregates agent metrics across a run."""

    records: list[AgentMetrics] = field(default_factory=list)

    def record(self, metrics: AgentMetrics) -> None:
        """Append a completed agent metrics record."""
        self.records.append(metrics)

    def pass_rate(self, *, model: str | None = None, role: str | None = None) -> float:
        """Fraction of tasks that passed, optionally filtered."""
        filtered = self._filter(model=model, role=role)
        if not filtered:
            return 0.0
        return sum(1 for m in filtered if m.result == "pass") / len(filtered)

    def avg_decision_latency(self) -> float:
        """Mean decision latency across all records with positive latency."""
        latencies = [m.decision_latency_s for m in self.records if m.decision_latency_s > 0]
        return sum(latencies) / len(latencies) if latencies else 0.0

    def retry_breakdown(self) -> dict[str, int]:
        """Count retries by category."""
        breakdown: dict[str, int] = {}
        for m in self.records:
            if m.retry_category:
                breakdown[m.retry_category] = breakdown.get(m.retry_category, 0) + 1
        return breakdown

    def compound_success_rate(self, avg_steps: float | None = None) -> float:
        """Calculate compound success rate: p_step ^ avg_steps."""
        if not self.records:
            return 0.0
        p_step = self.pass_rate()
        steps = avg_steps or self._avg_step_count()
        if steps <= 0:
            return p_step
        return p_step**steps

    def _avg_step_count(self) -> float:
        """Mean step count across records with positive step counts."""
        counts = [m.step_count for m in self.records if m.step_count > 0]
        return sum(counts) / len(counts) if counts else 1.0

    def _filter(self, *, model: str | None = None, role: str | None = None) -> list[AgentMetrics]:
        """Filter records by model and/or role."""
        result = self.records
        if model:
            result = [m for m in result if m.model == model]
        if role:
            result = [m for m in result if m.role == role]
        return result

    def to_summary(self) -> dict[str, object]:
        """Return a summary dict of key metrics."""
        return {
            "total_tasks": len(self.records),
            "pass_rate": round(self.pass_rate(), 3),
            "avg_decision_latency_s": round(self.avg_decision_latency(), 2),
            "compound_success_rate": round(self.compound_success_rate(), 3),
            "retry_breakdown": self.retry_breakdown(),
        }
