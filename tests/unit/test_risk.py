"""Tests for Strategic Risk Score (SRS) computation."""

from __future__ import annotations

import pytest

from bernstein.evolution.risk import (
    ProposalRiskScore,
    RiskScorer,
)

# ---------------------------------------------------------------------------
# ProposalRiskScore
# ---------------------------------------------------------------------------


def test_proposal_risk_score_fields() -> None:
    """ProposalRiskScore holds all required SRS fields."""
    score = ProposalRiskScore(
        code_complexity_delta=0.1,
        test_coverage_delta=-0.05,
        regression_potential=0.3,
        blast_radius=4,
        composite_risk=0.35,
    )
    assert score.code_complexity_delta == pytest.approx(0.1)
    assert score.test_coverage_delta == pytest.approx(-0.05)
    assert score.regression_potential == pytest.approx(0.3)
    assert score.blast_radius == 4
    assert score.composite_risk == pytest.approx(0.35)


def test_composite_risk_between_zero_and_one() -> None:
    """Composite risk must be clamped to [0.0, 1.0]."""
    scorer = RiskScorer()
    score = scorer.score_proposal(
        target_files=["src/foo.py"],
        diff_size=10,
        test_coverage_delta=0.05,
    )
    assert 0.0 <= score.composite_risk <= 1.0


# ---------------------------------------------------------------------------
# RiskScorer.score_proposal
# ---------------------------------------------------------------------------


def test_single_small_file_low_risk() -> None:
    """A single small diff on one file should produce low composite risk."""
    scorer = RiskScorer()
    score = scorer.score_proposal(
        target_files=["src/bernstein/config.py"],
        diff_size=15,
        test_coverage_delta=0.02,
    )
    assert score.composite_risk < 0.4
    assert score.blast_radius == 1


def test_many_files_high_blast_radius() -> None:
    """Touching many files should increase blast_radius."""
    scorer = RiskScorer()
    score = scorer.score_proposal(
        target_files=[f"src/file{i}.py" for i in range(10)],
        diff_size=200,
        test_coverage_delta=0.0,
    )
    assert score.blast_radius == 10


def test_large_diff_raises_complexity_delta() -> None:
    """Large diffs (>200 lines) indicate higher complexity delta."""
    scorer = RiskScorer()
    score_small = scorer.score_proposal(
        target_files=["src/foo.py"],
        diff_size=20,
        test_coverage_delta=0.0,
    )
    score_large = scorer.score_proposal(
        target_files=["src/foo.py"],
        diff_size=500,
        test_coverage_delta=0.0,
    )
    assert score_large.code_complexity_delta > score_small.code_complexity_delta


def test_negative_coverage_delta_raises_regression_potential() -> None:
    """Coverage regression should increase regression_potential."""
    scorer = RiskScorer()
    score_improve = scorer.score_proposal(
        target_files=["src/foo.py"],
        diff_size=50,
        test_coverage_delta=0.10,
    )
    score_regress = scorer.score_proposal(
        target_files=["src/foo.py"],
        diff_size=50,
        test_coverage_delta=-0.10,
    )
    assert score_regress.regression_potential > score_improve.regression_potential
    assert score_regress.test_coverage_delta < score_improve.test_coverage_delta


def test_coverage_improvement_reduces_risk() -> None:
    """Positive coverage delta should lower composite risk relative to zero delta."""
    scorer = RiskScorer()
    score_neutral = scorer.score_proposal(
        target_files=["src/foo.py"],
        diff_size=50,
        test_coverage_delta=0.0,
    )
    score_better = scorer.score_proposal(
        target_files=["src/foo.py"],
        diff_size=50,
        test_coverage_delta=0.15,
    )
    assert score_better.composite_risk <= score_neutral.composite_risk


def test_core_module_increases_regression_potential() -> None:
    """Touching core orchestrator/janitor files raises regression_potential."""
    scorer = RiskScorer()
    score_peripheral = scorer.score_proposal(
        target_files=["src/bernstein/adapters/claude.py"],
        diff_size=50,
        test_coverage_delta=0.0,
    )
    score_core = scorer.score_proposal(
        target_files=["src/bernstein/core/orchestrator.py"],
        diff_size=50,
        test_coverage_delta=0.0,
    )
    assert score_core.regression_potential > score_peripheral.regression_potential


# ---------------------------------------------------------------------------
# RiskScorer.is_high_risk
# ---------------------------------------------------------------------------


def test_is_high_risk_above_threshold() -> None:
    """Scores above the default 0.5 threshold are high risk."""
    scorer = RiskScorer()
    score = ProposalRiskScore(
        code_complexity_delta=0.8,
        test_coverage_delta=-0.2,
        regression_potential=0.9,
        blast_radius=15,
        composite_risk=0.75,
    )
    assert scorer.is_high_risk(score) is True


def test_is_high_risk_below_threshold() -> None:
    """Scores at or below the threshold are not high risk."""
    scorer = RiskScorer()
    score = ProposalRiskScore(
        code_complexity_delta=0.1,
        test_coverage_delta=0.05,
        regression_potential=0.1,
        blast_radius=1,
        composite_risk=0.25,
    )
    assert scorer.is_high_risk(score) is False


def test_is_high_risk_custom_threshold() -> None:
    """is_high_risk respects a custom threshold parameter."""
    scorer = RiskScorer()
    score = ProposalRiskScore(
        code_complexity_delta=0.3,
        test_coverage_delta=0.0,
        regression_potential=0.3,
        blast_radius=3,
        composite_risk=0.40,
    )
    assert scorer.is_high_risk(score, threshold=0.35) is True
    assert scorer.is_high_risk(score, threshold=0.50) is False


# ---------------------------------------------------------------------------
# RiskScorer: sandbox routing signal
# ---------------------------------------------------------------------------


def test_high_risk_proposal_flagged_for_sandbox() -> None:
    """Proposals above high-risk threshold should be flagged for sandbox."""
    scorer = RiskScorer()
    # Worst case: many core files, huge diff, coverage regresses
    score = scorer.score_proposal(
        target_files=[
            "src/bernstein/core/orchestrator.py",
            "src/bernstein/core/janitor.py",
            "src/bernstein/core/server.py",
        ],
        diff_size=800,
        test_coverage_delta=-0.15,
    )
    assert scorer.is_high_risk(score)


def test_low_risk_proposal_fast_tracked() -> None:
    """Small config-only changes should NOT be flagged as high risk."""
    scorer = RiskScorer()
    score = scorer.score_proposal(
        target_files=["templates/roles/backend.md"],
        diff_size=8,
        test_coverage_delta=0.0,
    )
    assert not scorer.is_high_risk(score)
