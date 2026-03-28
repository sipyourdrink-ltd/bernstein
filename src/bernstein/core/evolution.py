"""Backward-compatibility shim — delegates to bernstein.evolution package.

All classes and functions have been moved to risk-stratified modules under
bernstein/evolution/. This file re-exports them so existing imports continue
to work.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.evolution import (
    AgentMetrics,
    AnalysisEngine,
    AnalysisTrigger,
    AnomalyDetection,
    ApprovalMode,
    CostMetrics,
    EvolutionCoordinator,
    FileMetricsCollector,
    FileUpgradeExecutor,
    ImprovementOpportunity,
    MetricRecord,
    MetricsCollector,
    QualityMetrics,
    TaskMetrics,
    TrendAnalysis,
    UpgradeCategory,
    UpgradeExecutor,
    UpgradeProposal,
    UpgradeStatus,
)

__all__ = [
    "AgentMetrics",
    "AnalysisEngine",
    "AnalysisTrigger",
    "AnomalyDetection",
    "ApprovalMode",
    "CostMetrics",
    "EvolutionCoordinator",
    "FileMetricsCollector",
    "FileUpgradeExecutor",
    "ImprovementOpportunity",
    "MetricRecord",
    "MetricsCollector",
    "QualityMetrics",
    "TaskMetrics",
    "TrendAnalysis",
    "UpgradeCategory",
    "UpgradeExecutor",
    "UpgradeProposal",
    "UpgradeStatus",
    "get_default_coordinator",
]

# Own singleton so tests can reset via `bernstein.core.evolution._default_coordinator = None`
_default_coordinator: EvolutionCoordinator | None = None


def get_default_coordinator(
    state_dir: Path | None = None,
    analysis_interval_minutes: int = 60,
) -> EvolutionCoordinator:
    """Get or create the default evolution coordinator."""
    global _default_coordinator

    if _default_coordinator is None:
        if state_dir is None:
            state_dir = Path(".sdd")

        _default_coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            analysis_interval_minutes=analysis_interval_minutes,
        )

    return _default_coordinator
