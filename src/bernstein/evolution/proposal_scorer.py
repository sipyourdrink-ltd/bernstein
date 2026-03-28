"""Proposal risk scoring and routing classification.

Handles risk scoring computations and risk-based routing decisions for proposals
in the evolution loop. Maps proposal risk assessments to risk levels and
determines routing strategies based on composite risk scores.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.evolution.risk import RiskScorer
from bernstein.evolution.types import RiskLevel

if TYPE_CHECKING:
    from bernstein.evolution.proposals import UpgradeProposal


class ProposalScorer:
    """Scores and classifies proposals by risk level and routing strategy.

    Provides:
    - Composite risk scoring via RiskScorer
    - Risk level classification (L0-L3)
    - Risk-based routing decisions (fast_track, standard, sandbox_verify)
    """

    def __init__(self) -> None:
        self._risk_scorer = RiskScorer()

    def compute_risk(
        self,
        proposal: UpgradeProposal,
    ) -> tuple[float, RiskLevel]:
        """Compute composite risk score and map to RiskLevel.

        Args:
            proposal: The upgrade proposal to score.

        Returns:
            Tuple of (composite_risk_score, RiskLevel).
        """
        target_files = list(proposal.risk_assessment.affected_components)
        # Estimate diff size from proposed_change length (lines heuristic).
        diff_estimate = max(len(proposal.proposed_change) // 10, 10)
        risk_score = self._risk_scorer.score_proposal(
            target_files=target_files,
            diff_size=diff_estimate,
            test_coverage_delta=0.0,  # unknown pre-execution
        )
        risk_level = self._infer_risk_level(proposal)
        return risk_score.composite_risk, risk_level

    def classify_risk_route(self, composite_risk: float) -> str:
        """Map a composite risk score to a routing strategy.

        Thresholds:
          - composite_risk > 0.6 → ``sandbox_verify``  (forced sandbox)
          - composite_risk 0.3-0.6 → ``standard``       (normal flow)
          - composite_risk < 0.3 → ``fast_track``       (skip sandbox)

        Args:
            composite_risk: Composite risk score in [0.0, 1.0].

        Returns:
            One of ``"sandbox_verify"``, ``"standard"``, or ``"fast_track"``.
        """
        if composite_risk > 0.6:
            return "sandbox_verify"
        if composite_risk > 0.3:
            return "standard"
        return "fast_track"

    @staticmethod
    def infer_risk_level(proposal: UpgradeProposal) -> RiskLevel:
        """Map a proposal's risk assessment to a RiskLevel enum.

        The ProposalGenerator sets ``risk_assessment.level`` as a string
        ("low", "medium", "high"). We map these to evolution risk levels:
        - "low" → L0_CONFIG
        - "medium" → L1_TEMPLATE
        - "high" → L2_LOGIC (will be blocked by the automated loop)

        Args:
            proposal: The upgrade proposal to classify.

        Returns:
            Corresponding RiskLevel.
        """
        return ProposalScorer._infer_risk_level(proposal)

    @staticmethod
    def _infer_risk_level(proposal: UpgradeProposal) -> RiskLevel:
        """Internal implementation of risk level inference."""
        level_str = proposal.risk_assessment.level
        mapping: dict[str, RiskLevel] = {
            "low": RiskLevel.L0_CONFIG,
            "medium": RiskLevel.L1_TEMPLATE,
            "high": RiskLevel.L2_LOGIC,
            "critical": RiskLevel.L3_STRUCTURAL,
        }
        return mapping.get(level_str, RiskLevel.L2_LOGIC)
