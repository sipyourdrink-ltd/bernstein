"""API usage tracking and metrics collection.

Tracks API calls per provider, tier, session, and agent,
enabling cost analysis and tier-based scaling decisions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ApiCallRecord:
    """Record of a single API call for cost tracking.

    Attributes:
        provider: AI provider name (e.g., 'anthropic', 'openai').
        model: Model identifier (e.g., 'claude-3-opus').
        timestamp: ISO format timestamp.
        input_tokens: Tokens sent to the model.
        output_tokens: Tokens in the response.
        cost_usd: Estimated cost in USD.
        task_id: ID of the task that triggered the call.
        agent_id: ID of the agent making the call.
    """

    provider: str
    model: str
    timestamp: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    task_id: str = ""
    agent_id: str = ""


@dataclass
class ProviderUsageSummary:
    """Aggregated usage by provider.

    Attributes:
        provider: Provider name.
        calls: Number of calls.
        total_input_tokens: Total input tokens across calls.
        total_output_tokens: Total output tokens.
        total_cost_usd: Total cost in USD.
    """

    provider: str
    calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0


@dataclass
class AgentSessionUsage:
    """Usage metrics for a single agent session.

    Attributes:
        agent_id: Agent identifier.
        session_start: Start timestamp.
        calls: List of API call records.
        total_tokens: Sum of input + output tokens.
        total_cost_usd: Total cost for session.
    """

    agent_id: str
    session_start: str
    calls: list[ApiCallRecord] = field(default_factory=list)
    total_tokens: int = 0
    total_cost_usd: float = 0.0


@dataclass
class TierConsumption:
    """Tier-level consumption metrics.

    Attributes:
        tier: API tier name (e.g., 'fast', 'opus').
        task_count: Number of tasks completed at this tier.
        total_cost_usd: Total cost at this tier.
        avg_cost_per_task: Average cost per task.
    """

    tier: str
    task_count: int = 0
    total_cost_usd: float = 0.0

    @property
    def avg_cost_per_task(self) -> float:
        """Calculate average cost per task."""
        return self.total_cost_usd / self.task_count if self.task_count > 0 else 0.0


class ApiUsageTracker:
    """Tracks API calls, costs, and usage by provider/tier/agent.

    Provides per-session cost tracking and tier-aware metrics for
    making scaling and model-selection decisions.

    Attributes:
        metrics_dir: Directory for persisting metrics.
        calls: List of all recorded API calls.
        sessions: Dict of {agent_id: AgentSessionUsage}.
        tier_consumption: Dict of {tier: TierConsumption}.
    """

    def __init__(self, metrics_dir: Path | None = None) -> None:
        """Initialize the tracker.

        Args:
            metrics_dir: Optional directory for persisting metrics.
                Defaults to .sdd/metrics/.
        """
        self.metrics_dir = metrics_dir or Path(".sdd/metrics")
        self.calls: list[ApiCallRecord] = []
        self.sessions: dict[str, AgentSessionUsage] = {}
        self.tier_consumption: dict[str, TierConsumption] = {}

    def record_call(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        task_id: str = "",
        agent_id: str = "",
    ) -> None:
        """Record a single API call.

        Args:
            provider: Provider name.
            model: Model identifier.
            input_tokens: Tokens consumed.
            output_tokens: Tokens generated.
            cost_usd: Estimated cost.
            task_id: Associated task ID.
            agent_id: Associated agent ID.
        """
        record = ApiCallRecord(
            provider=provider,
            model=model,
            timestamp=datetime.utcnow().isoformat(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            task_id=task_id,
            agent_id=agent_id,
        )
        self.calls.append(record)

        # Update session
        if agent_id:
            if agent_id not in self.sessions:
                self.sessions[agent_id] = AgentSessionUsage(
                    agent_id=agent_id,
                    session_start=record.timestamp,
                )
            session = self.sessions[agent_id]
            session.calls.append(record)
            session.total_tokens += input_tokens + output_tokens
            session.total_cost_usd += cost_usd

        self.persist()

    def record_tier_usage(self, tier: str, task_id: str, cost_usd: float) -> None:
        """Record usage at a specific tier.

        Args:
            tier: Tier name (e.g., 'fast', 'opus').
            task_id: Task ID that used this tier.
            cost_usd: Cost for the task.
        """
        if tier not in self.tier_consumption:
            self.tier_consumption[tier] = TierConsumption(tier=tier)

        tc = self.tier_consumption[tier]
        tc.task_count += 1
        tc.total_cost_usd += cost_usd

        self.persist()

    def session_summary(self, agent_id: str) -> dict[str, Any]:
        """Get summary metrics for an agent session.

        Args:
            agent_id: Agent identifier.

        Returns:
            Dict with token counts, cost, call count.
        """
        if agent_id not in self.sessions:
            return {}

        session = self.sessions[agent_id]
        return {
            "agent_id": agent_id,
            "calls": len(session.calls),
            "total_tokens": session.total_tokens,
            "total_cost_usd": round(session.total_cost_usd, 4),
            "start": session.session_start,
        }

    def provider_summary(self) -> dict[str, ProviderUsageSummary]:
        """Get aggregated summary by provider.

        Returns:
            Dict mapping provider name to ProviderUsageSummary.
        """
        by_provider: dict[str, ProviderUsageSummary] = {}

        for call in self.calls:
            if call.provider not in by_provider:
                by_provider[call.provider] = ProviderUsageSummary(provider=call.provider)

            summary = by_provider[call.provider]
            summary.calls += 1
            summary.total_input_tokens += call.input_tokens
            summary.total_output_tokens += call.output_tokens
            summary.total_cost_usd += call.cost_usd

        return by_provider

    def tier_summary(self) -> dict[str, TierConsumption]:
        """Get tier consumption summary.

        Returns:
            Dict mapping tier name to TierConsumption.
        """
        return self.tier_consumption

    def total_cost(self) -> float:
        """Get total cost across all calls.

        Returns:
            Total cost in USD.
        """
        return sum(c.cost_usd for c in self.calls)

    def monthly_budget_remaining(self, monthly_limit_usd: float = 1000.0) -> float:
        """Calculate estimated remaining budget for the month.

        Args:
            monthly_limit_usd: Monthly budget limit in USD.

        Returns:
            Remaining budget (can be negative).
        """
        return monthly_limit_usd - self.total_cost()

    def is_over_budget(self, monthly_limit_usd: float = 1000.0) -> bool:
        """Check if spending exceeds monthly limit.

        Args:
            monthly_limit_usd: Monthly budget limit.

        Returns:
            True if over budget.
        """
        return self.total_cost() > monthly_limit_usd

    def persist(self) -> None:
        """Write metrics to disk (JSON format).

        Stores calls, sessions, and tier consumption in separate files
        under self.metrics_dir.
        """
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

        # Persist calls
        calls_file = self.metrics_dir / "api_calls.jsonl"
        with open(calls_file, "a", encoding="utf-8") as f:
            for call in self.calls[-1:]:  # Append only the latest call
                f.write(json.dumps(asdict(call)) + "\n")

        # Persist summary
        summary_file = self.metrics_dir / "summary.json"
        summary_data = {
            "total_calls": len(self.calls),
            "total_cost_usd": round(self.total_cost(), 4),
            "providers": {k: asdict(v) for k, v in self.provider_summary().items()},
            "tiers": {k: asdict(v) for k, v in self.tier_consumption.items()},
            "last_updated": datetime.utcnow().isoformat(),
        }
        summary_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")

        # Persist sessions
        sessions_file = self.metrics_dir / "sessions.json"
        sessions_data = {
            k: {
                "agent_id": v.agent_id,
                "session_start": v.session_start,
                "calls": len(v.calls),
                "total_tokens": v.total_tokens,
                "total_cost_usd": round(v.total_cost_usd, 4),
            }
            for k, v in self.sessions.items()
        }
        with open(sessions_file, "w", encoding="utf-8") as f:
            json.dump(sessions_data, f, indent=2)


# Global instance for easy access
_default_usage_tracker: ApiUsageTracker | None = None


def get_usage_tracker(metrics_dir: Path | None = None) -> ApiUsageTracker:
    """Get or create the default API usage tracker.

    Args:
        metrics_dir: Optional custom metrics directory.

    Returns:
        ApiUsageTracker instance.
    """
    global _default_usage_tracker
    if _default_usage_tracker is None:
        _default_usage_tracker = ApiUsageTracker(metrics_dir)
    return _default_usage_tracker
