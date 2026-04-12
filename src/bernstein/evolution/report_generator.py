"""Analysis result types, statistical helpers, and Goodhart's Law defenses.

Re-exports from the canonical definitions in ``aggregator.py`` for backward
compatibility.  New code should import directly from ``aggregator``.
"""

from __future__ import annotations

from bernstein.evolution.aggregator import (
    _COMPOSITE_WEIGHTS,
    _CORRELATED_PAIRS,
    MIN_SAMPLES_AB,
    MIN_SAMPLES_ALERTING,
    MIN_SAMPLES_BOCPD,
    MIN_SAMPLES_CUSUM,
    MIN_SAMPLES_EWMA,
    MIN_SAMPLES_MANN_KENDALL,
    MIN_SAMPLES_TREND,
    AnomalyDetection,
    BetaBinomialPosterior,
    Changepoint,
    CompositeScore,
    CUSUMState,
    EWMAState,
    NormalInverseGammaPosterior,
    TrendAnalysis,
    _bocpd_offline,
    _cusum_update,
    _ewma_control_limits,
    _ewma_update,
    _mann_kendall,
    _norm_cdf,
    _std,
    _student_t_pdf,
)

__all__ = [
    "MIN_SAMPLES_AB",
    "MIN_SAMPLES_ALERTING",
    "MIN_SAMPLES_BOCPD",
    "MIN_SAMPLES_CUSUM",
    "MIN_SAMPLES_EWMA",
    "MIN_SAMPLES_MANN_KENDALL",
    "MIN_SAMPLES_TREND",
    "_COMPOSITE_WEIGHTS",
    "_CORRELATED_PAIRS",
    "AnomalyDetection",
    "BetaBinomialPosterior",
    "CUSUMState",
    "Changepoint",
    "CompositeScore",
    "EWMAState",
    "NormalInverseGammaPosterior",
    "TrendAnalysis",
    "_bocpd_offline",
    "_cusum_update",
    "_ewma_control_limits",
    "_ewma_update",
    "_mann_kendall",
    "_norm_cdf",
    "_std",
    "_student_t_pdf",
]
