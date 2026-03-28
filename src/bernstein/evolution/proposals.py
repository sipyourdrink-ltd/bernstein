"""Upgrade proposal generation."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from bernstein.core.models import (
    Complexity,
    RiskAssessment,
    RollbackPlan,
    Scope,
    Task,
    TaskType,
    UpgradeProposalDetails,
)
from bernstein.evolution.detector import ImprovementOpportunity, UpgradeCategory

if TYPE_CHECKING:
    from bernstein.evolution.aggregator import AnomalyDetection


class AnalysisTrigger(Enum):
    """Triggers for running analysis."""
    SCHEDULED = "scheduled"  # Periodic scheduled run
    THRESHOLD = "threshold"  # Metric threshold exceeded
    MANUAL = "manual"  # Manual trigger
    ANOMALY = "anomaly"  # Anomaly detected


class UpgradeStatus(Enum):
    """Status of an upgrade."""
    PENDING = "pending"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


class ApprovalMode(Enum):
    """Approval mode for upgrades."""
    AUTO = "auto"  # Apply immediately
    HUMAN = "human"  # Require human approval
    HYBRID = "hybrid"  # Auto if confidence > 90%


@dataclass
class UpgradeProposal:
    """Proposal for a system upgrade."""
    id: str
    title: str
    category: UpgradeCategory
    description: str
    current_state: str
    proposed_change: str
    benefits: list[str]
    risk_assessment: RiskAssessment
    rollback_plan: RollbackPlan
    cost_estimate_usd: float
    expected_improvement: str
    confidence: float
    status: UpgradeStatus = UpgradeStatus.PENDING
    approval_mode: ApprovalMode = ApprovalMode.HYBRID
    created_at: float = field(default_factory=time.time)
    applied_at: float | None = None
    triggered_by: AnalysisTrigger = AnalysisTrigger.SCHEDULED

    def to_task(self) -> Task:
        """Convert upgrade proposal to a Task."""
        upgrade_details = UpgradeProposalDetails(
            current_state=self.current_state,
            proposed_change=self.proposed_change,
            benefits=self.benefits,
            risk_assessment=self.risk_assessment,
            rollback_plan=self.rollback_plan,
            cost_estimate_usd=self.cost_estimate_usd,
            performance_impact=self.expected_improvement,
        )

        return Task(
            id=self.id,
            title=self.title,
            description=self.description,
            role="manager",  # Manager handles upgrades
            priority=1 if self.risk_assessment.level == "critical" else 2,
            scope=Scope.MEDIUM,
            complexity=Complexity.HIGH,
            estimated_minutes=60,
            task_type=TaskType.UPGRADE_PROPOSAL,
            upgrade_details=upgrade_details,
        )


class ProposalGenerator:
    """Creates upgrade proposals from opportunities and anomalies."""

    def __init__(self) -> None:
        self._counter: int = 0

    def create_proposal(
        self,
        opportunity: ImprovementOpportunity,
        trigger: AnalysisTrigger,
    ) -> UpgradeProposal:
        """Create an upgrade proposal from an opportunity."""
        self._counter += 1
        proposal_id = f"UPG-{self._counter:04d}"

        risk_level_map = {
            "low": RiskAssessment(level="low"),
            "medium": RiskAssessment(level="medium"),
            "high": RiskAssessment(level="high"),
        }

        approval_mode_map = {
            "low": ApprovalMode.AUTO,
            "medium": ApprovalMode.HYBRID,
            "high": ApprovalMode.HUMAN,
        }

        return UpgradeProposal(
            id=proposal_id,
            title=opportunity.title,
            category=opportunity.category,
            description=opportunity.description,
            current_state=f"Current {opportunity.category.value} needs improvement",
            proposed_change=opportunity.description,
            benefits=[opportunity.expected_improvement],
            risk_assessment=risk_level_map.get(opportunity.risk_level, RiskAssessment()),
            rollback_plan=RollbackPlan(
                steps=["Revert configuration changes", "Restart affected components"],
                estimated_rollback_minutes=15,
            ),
            cost_estimate_usd=opportunity.estimated_cost_impact_usd,
            expected_improvement=opportunity.expected_improvement,
            confidence=opportunity.confidence,
            approval_mode=approval_mode_map.get(opportunity.risk_level, ApprovalMode.HYBRID),
            triggered_by=trigger,
        )

    def create_emergency_proposal(
        self,
        anomaly: AnomalyDetection,
    ) -> UpgradeProposal:
        """Create an emergency proposal for a critical anomaly."""
        self._counter += 1
        proposal_id = f"EMG-{self._counter:04d}"

        return UpgradeProposal(
            id=proposal_id,
            title=f"Emergency fix for {anomaly.metric_name} anomaly",
            category=UpgradeCategory.POLICY_UPDATE,
            description=anomaly.description,
            current_state=f"Critical anomaly detected in {anomaly.metric_name}",
            proposed_change=f"Investigate and fix {anomaly.metric_name} anomaly",
            benefits=["Restore normal operation", "Prevent further degradation"],
            risk_assessment=RiskAssessment(level="high", affected_components=[anomaly.metric_name]),
            rollback_plan=RollbackPlan(
                steps=["Revert any changes", "Investigate root cause"],
                estimated_rollback_minutes=30,
            ),
            cost_estimate_usd=0.0,
            expected_improvement="Restore baseline metrics",
            confidence=0.9,
            approval_mode=ApprovalMode.AUTO,
            triggered_by=AnalysisTrigger.ANOMALY,
        )
