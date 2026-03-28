"""Strategic Risk Score (SRS) computation for evolution proposals.

Computes a composite risk score for each proposal before applying it.
High-risk proposals are routed to sandbox verification; low-risk proposals
are fast-tracked.  This replaces the binary apply/reject gate with a
graduated risk-aware pipeline.

Risk dimensions:
  - code_complexity_delta  — did the diff increase apparent complexity?
  - test_coverage_delta    — did coverage improve or regress?
  - regression_potential   — how likely are existing tests to break?
  - blast_radius           — how many files were touched?
  - composite_risk         — weighted combination, clamped to [0.0, 1.0]
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Core module paths that carry higher regression potential when touched
# ---------------------------------------------------------------------------
_HIGH_REGRESSION_MODULES = frozenset(
    {
        "orchestrator",
        "janitor",
        "server",
        "invariants",
        "circuit",
        "bootstrap",
    }
)

# Thresholds for complexity classification
_LARGE_DIFF_LINES = 200  # above this → high complexity delta

# Composite risk weights
_W_COMPLEXITY = 0.25
_W_COVERAGE = 0.25
_W_REGRESSION = 0.30
_W_BLAST = 0.20

# Blast-radius normalisation: cap at this many files for scoring purposes
_BLAST_CAP = 20


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ProposalRiskScore:
    """Risk assessment for a single evolution proposal.

    Attributes:
        code_complexity_delta: Normalised complexity increase (0.0-1.0).
            Higher means the diff is larger / more structurally complex.
        test_coverage_delta: Signed coverage change (negative = regression).
            Raw value passed through from the sandbox metrics.
        regression_potential: Probability (0.0-1.0) that the change will
            break existing tests, based on which modules are touched.
        blast_radius: Raw count of files touched by the proposal.
        composite_risk: Weighted combination of all dimensions, clamped to
            [0.0, 1.0].
    """

    code_complexity_delta: float
    test_coverage_delta: float
    regression_potential: float
    blast_radius: int
    composite_risk: float


# ---------------------------------------------------------------------------
# RiskScorer
# ---------------------------------------------------------------------------


class RiskScorer:
    """Computes Strategic Risk Scores for evolution proposals.

    Usage::

        scorer = RiskScorer()
        score = scorer.score_proposal(
            target_files=["src/bernstein/core/orchestrator.py"],
            diff_size=350,
            test_coverage_delta=-0.05,
        )
        if scorer.is_high_risk(score):
            route_to_sandbox(proposal)
        else:
            fast_track(proposal)
    """

    # Default composite-risk threshold for high-risk classification
    DEFAULT_HIGH_RISK_THRESHOLD: float = 0.50

    def score_proposal(
        self,
        target_files: list[str],
        diff_size: int,
        test_coverage_delta: float,
    ) -> ProposalRiskScore:
        """Compute a risk score for a proposal.

        Args:
            target_files: List of file paths modified by the proposal.
            diff_size: Total lines changed (added + removed) in the diff.
            test_coverage_delta: Change in test coverage fraction
                (positive = coverage improved, negative = regression).

        Returns:
            A ``ProposalRiskScore`` with all dimensions populated.
        """
        blast_radius = len(target_files)

        # 1. Complexity delta: logarithmic scale capped at 1.0
        if diff_size <= 0:
            complexity_delta = 0.0
        elif diff_size <= _LARGE_DIFF_LINES:
            # 0 → 0.0, LARGE_DIFF_LINES → 0.5 (linear)
            complexity_delta = (diff_size / _LARGE_DIFF_LINES) * 0.5
        else:
            # Above threshold: 0.5 → 1.0 logarithmically
            import math

            excess = diff_size - _LARGE_DIFF_LINES
            complexity_delta = 0.5 + 0.5 * min(math.log1p(excess) / math.log1p(2000), 1.0)

        # 2. Regression potential: base score from file criticality + blast
        base_regression = 0.0
        for path in target_files:
            # Check if any path segment matches a high-regression module name
            parts = path.replace("\\", "/").split("/")
            stem = parts[-1].removesuffix(".py") if parts else ""
            if stem in _HIGH_REGRESSION_MODULES:
                base_regression = max(base_regression, 0.70)
            elif "core" in parts:
                base_regression = max(base_regression, 0.40)

        # Blast-radius contribution to regression potential
        blast_factor = min(blast_radius / _BLAST_CAP, 1.0) * 0.30
        # Coverage regression raises the chance that tests break
        coverage_regression_factor = max(0.0, -test_coverage_delta) * 0.20
        regression_potential = min(base_regression + blast_factor + coverage_regression_factor, 1.0)

        # 3. Normalise blast radius for scoring
        blast_score = min(blast_radius / _BLAST_CAP, 1.0)

        # 4. Coverage component: negative delta → risk, positive → reduces risk
        # Map coverage_delta ∈ [-1, +1] to risk ∈ [1, 0]
        coverage_risk = max(0.0, 0.5 - test_coverage_delta)
        coverage_risk = min(coverage_risk, 1.0)

        # 5. Composite
        composite = (
            _W_COMPLEXITY * complexity_delta
            + _W_COVERAGE * coverage_risk
            + _W_REGRESSION * regression_potential
            + _W_BLAST * blast_score
        )
        composite = max(0.0, min(1.0, composite))

        return ProposalRiskScore(
            code_complexity_delta=complexity_delta,
            test_coverage_delta=test_coverage_delta,
            regression_potential=regression_potential,
            blast_radius=blast_radius,
            composite_risk=composite,
        )

    def is_high_risk(
        self,
        score: ProposalRiskScore,
        threshold: float = DEFAULT_HIGH_RISK_THRESHOLD,
    ) -> bool:
        """Return True if the composite risk exceeds *threshold*.

        Args:
            score: The risk score to evaluate.
            threshold: Composite-risk cutoff; defaults to 0.5.

        Returns:
            ``True`` if the proposal should be routed to sandbox verification.
        """
        return score.composite_risk > threshold
