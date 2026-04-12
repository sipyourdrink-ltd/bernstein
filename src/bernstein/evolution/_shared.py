"""Shared constants, data classes, and helpers for the evolution loop modules.

Extracted from cycle_runner.py and loop.py to eliminate code duplication.
Both modules import from here instead of redefining the same symbols.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.evolution.types import RiskLevel, SandboxResult
from bernstein.evolution.types import UpgradeProposal as TypesUpgradeProposal

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.evolution.proposals import UpgradeProposal

logger = logging.getLogger(__name__)

# Cost estimate per proposal generation (LLM call).
COST_PER_PROPOSAL_USD = 0.05

# Risk levels eligible for the automated loop.
AUTO_RISK_LEVELS: frozenset[str] = frozenset(
    {
        RiskLevel.L0_CONFIG.value,
        RiskLevel.L1_TEMPLATE.value,
    }
)

# Focus area rotation — creative_vision runs every 4th cycle.
# Agents write proposals to .sdd/evolution/creative/pending_proposals.jsonl;
# the loop picks them up on creative_vision turns.
FOCUS_ROTATION: tuple[str, ...] = (
    "code_quality",
    "test_coverage",
    "performance",
    "creative_vision",
)

# In community mode the first slot of each rotation is replaced with a
# community_issue scan so community work gets priority.
FOCUS_ROTATION_COMMUNITY: tuple[str, ...] = (
    "community_issue",
    "test_coverage",
    "performance",
    "creative_vision",
)


@dataclass
class ExperimentResult:
    """Outcome of a single autoresearch experiment cycle.

    Attributes:
        proposal_id: Unique identifier for the proposal tested.
        title: Human-readable title of the proposal.
        risk_level: String risk classification (from RiskLevel.value).
        baseline_score: Benchmark score before the experiment.
        candidate_score: Benchmark score after applying the proposal.
        delta: Difference (candidate - baseline).
        accepted: Whether the proposal was applied.
        reason: Explanation for the accept/discard decision.
        cost_usd: Estimated cost of the experiment.
        duration_seconds: Wall-clock time of the experiment cycle.
        timestamp: Unix timestamp when the result was recorded.
    """

    proposal_id: str
    title: str
    risk_level: str
    baseline_score: float
    candidate_score: float
    delta: float
    accepted: bool
    reason: str
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSONL output."""
        return {
            "proposal_id": self.proposal_id,
            "title": self.title,
            "risk_level": self.risk_level,
            "baseline_score": self.baseline_score,
            "candidate_score": self.candidate_score,
            "delta": self.delta,
            "accepted": self.accepted,
            "reason": self.reason,
            "cost_usd": self.cost_usd,
            "duration_seconds": self.duration_seconds,
            "timestamp": self.timestamp,
        }


def to_types_proposal(
    proposal: UpgradeProposal,
    risk_level: RiskLevel,
) -> TypesUpgradeProposal:
    """Convert a proposals.UpgradeProposal to a types.UpgradeProposal for the gate.

    The ApprovalGate.route() expects bernstein.evolution.types.UpgradeProposal
    which has different fields than proposals.UpgradeProposal. This adapter
    bridges the two schemas.

    Args:
        proposal: The proposals-module UpgradeProposal.
        risk_level: The classified RiskLevel.

    Returns:
        A types-module UpgradeProposal suitable for the approval gate.
    """
    return TypesUpgradeProposal(
        id=proposal.id,
        title=proposal.title,
        description=proposal.description,
        risk_level=risk_level,
        target_files=[],  # No specific file targets for generated proposals.
        diff=proposal.proposed_change,
        rationale=proposal.description,
        expected_impact=proposal.expected_improvement,
        confidence=proposal.confidence,
    )


def make_fast_track_sandbox_result(
    proposal_id: str,
    baseline_score: float,
) -> SandboxResult:
    """Return a synthetic passed SandboxResult for fast-tracked proposals.

    Fast-tracked proposals (composite_risk < 0.3) skip sandbox validation.
    We create a neutral result so the apply path can proceed normally.

    Args:
        proposal_id: ID of the fast-tracked proposal.
        baseline_score: Current baseline benchmark score.

    Returns:
        A ``SandboxResult`` marked as passed with no test data.
    """
    return SandboxResult(
        proposal_id=proposal_id,
        passed=True,
        tests_passed=0,
        tests_failed=0,
        tests_total=0,
        baseline_score=baseline_score,
        candidate_score=baseline_score,
        delta=0.0,
        duration_seconds=0.0,
        log_path="",
    )


def log_experiment(experiments_path: Path, result: ExperimentResult) -> None:
    """Append experiment result to experiments.jsonl.

    Args:
        experiments_path: Path to the experiments.jsonl file.
        result: The experiment result to log.
    """
    try:
        with experiments_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_dict()) + "\n")
    except OSError:
        logger.exception("Failed to write experiment log")


def log_deferred(deferred_path: Path, proposal: UpgradeProposal, reason: str) -> None:
    """Log a deferred proposal to deferred.jsonl for human review.

    Args:
        deferred_path: Path to the deferred.jsonl file.
        proposal: The deferred proposal.
        reason: Reason for deferral.
    """
    record = {
        "proposal_id": proposal.id,
        "title": proposal.title,
        "category": proposal.category.value,
        "description": proposal.description,
        "confidence": proposal.confidence,
        "reason": reason,
        "deferred_at": time.time(),
    }
    try:
        with deferred_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        logger.info("Proposal %s deferred for human review", proposal.id)
    except OSError:
        logger.exception("Failed to write deferred proposal log")


def infer_risk_level(proposal: UpgradeProposal) -> RiskLevel:
    """Map a proposal's risk assessment to a RiskLevel enum.

    The ProposalGenerator sets ``risk_assessment.level`` as a string
    ("low", "medium", "high"). We map these to evolution risk levels:
    - "low" -> L0_CONFIG
    - "medium" -> L1_TEMPLATE
    - "high" -> L2_LOGIC (will be blocked by the automated loop)

    Args:
        proposal: The upgrade proposal to classify.

    Returns:
        Corresponding RiskLevel.
    """
    level_str = proposal.risk_assessment.level
    mapping: dict[str, RiskLevel] = {
        "low": RiskLevel.L0_CONFIG,
        "medium": RiskLevel.L1_TEMPLATE,
        "high": RiskLevel.L2_LOGIC,
        "critical": RiskLevel.L3_STRUCTURAL,
    }
    return mapping.get(level_str, RiskLevel.L2_LOGIC)
