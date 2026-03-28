"""Opportunity detection from aggregated metrics."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from bernstein.evolution.aggregator import MetricsCollector


class UpgradeCategory(Enum):
    """Category of upgrade."""
    POLICY_UPDATE = "policy_update"  # Low risk policy tweaks
    ROUTING_RULES = "routing_rules"  # Model routing adjustments
    MODEL_ROUTING = "model_routing"  # Model selection changes
    ROLE_TEMPLATES = "role_templates"  # Prompt template updates
    PROVIDER_CONFIG = "provider_config"  # Provider configuration


@dataclass
class ImprovementOpportunity:
    """Identified improvement opportunity."""
    category: UpgradeCategory
    title: str
    description: str
    expected_improvement: str
    confidence: float
    risk_level: Literal["low", "medium", "high"]
    affected_components: list[str] = field(default_factory=list)
    estimated_cost_impact_usd: float = 0.0


@dataclass
class FailurePattern:
    """A detected pattern of recurring failures.

    Groups failures by role and error type to identify systematic issues
    that can be addressed through configuration or template changes.
    """

    task_type: str
    error_pattern: str
    occurrence_count: int
    affected_models: list[str]
    first_seen: float
    last_seen: float
    sample_task_ids: list[str] = field(default_factory=list)


@dataclass
class FailureRecord:
    """A single failure event for JSONL persistence."""

    timestamp: float
    task_id: str
    role: str
    model: str | None
    error_type: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "role": self.role,
            "model": self.model,
            "error_type": self.error_type,
        }


class FailureAnalyzer:
    """Tracks and analyzes task failures to detect recurring patterns.

    Persists failure records to `.sdd/evolution/failures.jsonl` and provides
    methods to detect patterns, compute failure rates by role/model, and
    surface actionable insights for the evolution loop.
    """

    def __init__(self, state_dir: Path | None = None) -> None:
        if state_dir is None:
            state_dir = Path(".sdd")
        self._evolution_dir = state_dir / "evolution"
        self._evolution_dir.mkdir(parents=True, exist_ok=True)
        self.failures_path = self._evolution_dir / "failures.jsonl"
        self._failures: list[FailureRecord] = []
        self._load()

    def _load(self) -> None:
        """Load existing failure records from disk."""
        if not self.failures_path.exists():
            return
        with self.failures_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    self._failures.append(FailureRecord(
                        timestamp=data["timestamp"],
                        task_id=data["task_id"],
                        role=data["role"],
                        model=data.get("model"),
                        error_type=data["error_type"],
                    ))
                except (json.JSONDecodeError, KeyError):
                    continue

    def record_failure(
        self,
        task_id: str,
        role: str,
        model: str | None,
        error_type: str,
    ) -> None:
        """Record a task failure to disk and in-memory list.

        Args:
            task_id: Unique identifier of the failed task.
            role: The role that was assigned to the task.
            model: The model used, or None if unknown.
            error_type: Short description of the error category.
        """
        record = FailureRecord(
            timestamp=time.time(),
            task_id=task_id,
            role=role,
            model=model,
            error_type=error_type,
        )
        self._failures.append(record)
        with self.failures_path.open("a") as f:
            f.write(json.dumps(record.to_dict()) + "\n")

    def detect_patterns(self, min_occurrences: int = 3) -> list[FailurePattern]:
        """Detect recurring failure patterns by grouping on (role, error_type).

        Args:
            min_occurrences: Minimum number of failures to qualify as a pattern.

        Returns:
            List of failure patterns meeting the occurrence threshold.
        """
        groups: dict[tuple[str, str], list[FailureRecord]] = {}
        for record in self._failures:
            key = (record.role, record.error_type)
            groups.setdefault(key, []).append(record)

        patterns: list[FailurePattern] = []
        for (role, error_type), records in groups.items():
            if len(records) < min_occurrences:
                continue
            models = list({r.model for r in records if r.model is not None})
            task_ids = [r.task_id for r in records[:5]]
            patterns.append(FailurePattern(
                task_type=role,
                error_pattern=error_type,
                occurrence_count=len(records),
                affected_models=models,
                first_seen=records[0].timestamp,
                last_seen=records[-1].timestamp,
                sample_task_ids=task_ids,
            ))
        return patterns

    def get_failure_rate_by_role(self, hours: int = 24) -> dict[str, float]:
        """Compute failure rate per role over a recent time window.

        Args:
            hours: Number of hours to look back.

        Returns:
            Mapping of role name to failure rate (0.0-1.0).  The rate is
            computed as failures / total failures across all roles in the
            window, giving a relative distribution.  Returns 1.0 for each
            role since all records here are failures; pair with task metrics
            for absolute rates.
        """
        cutoff = time.time() - (hours * 3600)
        recent = [r for r in self._failures if r.timestamp >= cutoff]
        if not recent:
            return {}

        role_counts: dict[str, int] = {}
        for r in recent:
            role_counts[r.role] = role_counts.get(r.role, 0) + 1

        total = len(recent)
        return {role: count / total for role, count in role_counts.items()}

    def get_failure_rate_by_model(self, hours: int = 24) -> dict[str, float]:
        """Compute failure rate per model over a recent time window.

        Args:
            hours: Number of hours to look back.

        Returns:
            Mapping of model name to failure rate (0.0-1.0).  Same
            distribution semantics as ``get_failure_rate_by_role``.
        """
        cutoff = time.time() - (hours * 3600)
        recent = [r for r in self._failures if r.timestamp >= cutoff]
        if not recent:
            return {}

        model_counts: dict[str, int] = {}
        for r in recent:
            model_name = r.model if r.model is not None else "unknown"
            model_counts[model_name] = model_counts.get(model_name, 0) + 1

        total = len(recent)
        return {model: count / total for model, count in model_counts.items()}


class OpportunityDetector:
    """Identifies improvement opportunities from metrics."""

    def __init__(
        self,
        collector: MetricsCollector,
        failure_analyzer: FailureAnalyzer | None = None,
        analysis_dir: Path | None = None,
    ) -> None:
        self.collector = collector
        self.failure_analyzer = failure_analyzer
        self._analysis_dir = analysis_dir

    def identify_opportunities(self) -> list[ImprovementOpportunity]:
        """Identify improvement opportunities from recent metrics."""
        opportunities: list[ImprovementOpportunity] = []

        # Check for cost optimization opportunities
        cost_metrics = self.collector.get_recent_cost_metrics(hours=24)

        paid_providers = [m for m in cost_metrics if m.tier != "free"]
        if paid_providers:
            total_paid_cost = sum(m.cost_usd for m in paid_providers)
            if total_paid_cost > 1.0:  # More than $1 spent
                opportunities.append(ImprovementOpportunity(
                    category=UpgradeCategory.ROUTING_RULES,
                    title="Optimize free tier utilization",
                    description="Consider routing more tasks to free tier providers",
                    expected_improvement=f"Potential savings of ${total_paid_cost * 0.3:.2f}/day",
                    confidence=0.7,
                    risk_level="low",
                    estimated_cost_impact_usd=-total_paid_cost * 0.3,
                ))

        # Check for success rate improvements
        task_metrics = self.collector.get_recent_task_metrics(hours=24)
        if task_metrics:
            pass_rate = sum(1 for m in task_metrics if m.janitor_passed) / len(task_metrics)
            if pass_rate < 0.8:
                opportunities.append(ImprovementOpportunity(
                    category=UpgradeCategory.MODEL_ROUTING,
                    title="Improve task success rate",
                    description=f"Current success rate is {pass_rate:.1%}, target is 80%",
                    expected_improvement="Higher quality output, fewer fix tasks",
                    confidence=0.8,
                    risk_level="medium",
                    affected_components=["model_routing", "task_verification"],
                ))

        # Check for failure-driven opportunities
        opportunities.extend(self.identify_failure_opportunities())

        if self._analysis_dir is not None:
            self._write_opportunities(opportunities)

        return opportunities

    def _write_opportunities(self, opportunities: list[ImprovementOpportunity]) -> None:
        """Write detected opportunities to .sdd/analysis/opportunities.json."""
        if self._analysis_dir is None:
            return
        try:
            self._analysis_dir.mkdir(parents=True, exist_ok=True)
            opportunities_path = self._analysis_dir / "opportunities.json"
            data = {
                "generated_at": time.time(),
                "count": len(opportunities),
                "opportunities": [asdict(o) for o in opportunities],
            }
            opportunities_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("Failed to write opportunities to %s", self._analysis_dir)

    def identify_failure_opportunities(self) -> list[ImprovementOpportunity]:
        """Identify improvement opportunities from recurring failure patterns.

        Analyzes detected failure patterns and generates targeted suggestions:
        - Single-model failures -> MODEL_ROUTING change
        - Single-role failures -> ROLE_TEMPLATES change
        - Broad failures -> POLICY_UPDATE

        Returns:
            List of improvement opportunities derived from failure analysis.
        """
        if self.failure_analyzer is None:
            return []

        patterns = self.failure_analyzer.detect_patterns()
        opportunities: list[ImprovementOpportunity] = []

        for pattern in patterns:
            if pattern.occurrence_count < 3:
                continue

            if len(pattern.affected_models) == 1:
                # Failures concentrated on a single model — route away from it
                model = pattern.affected_models[0]
                opportunities.append(ImprovementOpportunity(
                    category=UpgradeCategory.MODEL_ROUTING,
                    title=f"Route {pattern.task_type} tasks away from {model}",
                    description=(
                        f"'{pattern.error_pattern}' occurred {pattern.occurrence_count} "
                        f"times on {model} for role {pattern.task_type}"
                    ),
                    expected_improvement=f"Reduce {pattern.task_type} failures by routing to alternative models",
                    confidence=min(0.9, 0.5 + pattern.occurrence_count * 0.05),
                    risk_level="medium",
                    affected_components=["model_routing"],
                ))
            elif pattern.task_type and len(pattern.affected_models) != 1:
                # Failures spread across models but tied to a role — fix the template
                if len(pattern.affected_models) <= 1:
                    # Should not happen given outer condition, but defensive
                    category = UpgradeCategory.POLICY_UPDATE
                else:
                    category = UpgradeCategory.ROLE_TEMPLATES
                opportunities.append(ImprovementOpportunity(
                    category=category,
                    title=f"Update template for {pattern.task_type} role",
                    description=(
                        f"'{pattern.error_pattern}' occurred {pattern.occurrence_count} "
                        f"times across models {', '.join(pattern.affected_models)} "
                        f"for role {pattern.task_type}"
                    ),
                    expected_improvement=f"Reduce recurring '{pattern.error_pattern}' failures",
                    confidence=min(0.9, 0.5 + pattern.occurrence_count * 0.05),
                    risk_level="medium",
                    affected_components=["role_templates", pattern.task_type],
                ))
            else:
                # Broad pattern — suggest policy review
                opportunities.append(ImprovementOpportunity(
                    category=UpgradeCategory.POLICY_UPDATE,
                    title=f"Review policy for '{pattern.error_pattern}' failures",
                    description=(
                        f"'{pattern.error_pattern}' occurred {pattern.occurrence_count} "
                        f"times across multiple roles and models"
                    ),
                    expected_improvement="Reduce systemic failure rate",
                    confidence=min(0.85, 0.4 + pattern.occurrence_count * 0.05),
                    risk_level="high",
                    affected_components=["policy"],
                ))

        return opportunities
