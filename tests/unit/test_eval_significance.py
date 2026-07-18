"""Tests for the pure statistics layer behind statistical eval gating (#2520).

The module provides exact paired tests for pass/fail outcomes, Wilson score
intervals, a fixed minimum-n rule per verdict strength, and canonical rounding
so verdicts are a pure, bit-stable function of the evidence.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

import pytest

from bernstein.eval.significance import (
    DEFAULT_ALPHA,
    DEFAULT_MIN_N,
    SUPPORTED_ALPHAS,
    PairedTable,
    Verdict,
    binom_tail_ge,
    canonical_round,
    classify,
    result_set_hash,
    suite_content_hash,
    wilson_interval,
)

# ---------------------------------------------------------------------------
# Exact binomial tail
# ---------------------------------------------------------------------------


def test_binom_tail_ge_is_exact_fraction() -> None:
    # P(X >= 5) for Binom(5, 1/2) == 1/32.
    assert binom_tail_ge(5, 5) == Fraction(1, 32)
    # P(X >= 0) is the whole mass.
    assert binom_tail_ge(0, 5) == Fraction(1, 1)
    # P(X >= 3) for Binom(4, 1/2) == (4 + 1)/16 == 5/16.
    assert binom_tail_ge(3, 4) == Fraction(5, 16)


def test_binom_tail_ge_zero_trials() -> None:
    assert binom_tail_ge(0, 0) == Fraction(1, 1)


def test_binom_tail_ge_rejects_bad_args() -> None:
    with pytest.raises(ValueError, match="k"):
        binom_tail_ge(-1, 5)
    with pytest.raises(ValueError, match="n"):
        binom_tail_ge(2, -1)


# ---------------------------------------------------------------------------
# Wilson interval
# ---------------------------------------------------------------------------


def test_wilson_interval_bounds_are_ordered_and_in_unit() -> None:
    z = SUPPORTED_ALPHAS[DEFAULT_ALPHA]
    low, high = wilson_interval(8, 10, z)
    assert 0.0 <= low <= high <= 1.0


def test_wilson_interval_zero_n_is_full_unit() -> None:
    z = SUPPORTED_ALPHAS[DEFAULT_ALPHA]
    low, high = wilson_interval(0, 0, z)
    assert (low, high) == (0.0, 1.0)


# ---------------------------------------------------------------------------
# Canonical rounding (bit stability)
# ---------------------------------------------------------------------------


def test_canonical_round_is_half_even_and_stable() -> None:
    # round-half-to-even at 10 places.
    assert canonical_round(0.12345678905) == canonical_round(0.12345678905)
    assert canonical_round(1 / 3) == canonical_round(1 / 3)
    # A value already at fewer places is unchanged.
    assert canonical_round(0.5) == 0.5


def test_canonical_round_normalises_negative_zero_without_a_tolerance() -> None:
    """Negative zero is flattened by sign, not by a magnitude comparison.

    The sign fix must not behave like an ``isclose``-to-zero test: a value that
    quantises to a genuinely non-zero p-value has to survive intact, otherwise
    the verdict changes.
    """
    # Every representation of zero comes back as positive zero.
    for value in (-0.0, 0.0, -1e-15, 1e-15):
        result = canonical_round(value)
        assert result == 0.0
        # ``math.copysign`` is the only way to tell 0.0 from -0.0 by value.
        assert math.copysign(1.0, result) == 1.0

    # The smallest magnitude that survives quantisation at 10 places is kept
    # exactly, with its sign. A tolerance comparison would flatten these.
    assert canonical_round(1e-10) == 1e-10
    assert canonical_round(-1e-10) == -1e-10
    assert math.copysign(1.0, canonical_round(-1e-10)) == -1.0


def test_canonical_round_is_bit_identical_to_the_quantised_decimal() -> None:
    """Rounding stays a pure quantise plus sign fix, so replays are stable."""
    for value in (0.1, 1 / 3, 0.12345678905, 2.5, -7.25, 1e-9, -1e-9):
        expected = float(Decimal(value).quantize(Decimal(1).scaleb(-10), rounding=ROUND_HALF_EVEN))
        result = canonical_round(value)
        # Bit-identical, not merely close.
        assert result.hex() == expected.hex()


# ---------------------------------------------------------------------------
# PairedTable construction and order invariance
# ---------------------------------------------------------------------------


def test_paired_table_from_outcomes_is_order_invariant() -> None:
    base = {"t1": True, "t2": False, "t3": True, "t4": False}
    cand = {"t1": True, "t2": True, "t3": True, "t4": True}
    forward = PairedTable.from_outcomes(base, cand)
    # Reversed insertion order must produce the identical table.
    base_rev = dict(reversed(list(base.items())))
    cand_rev = dict(reversed(list(cand.items())))
    reverse = PairedTable.from_outcomes(base_rev, cand_rev)
    assert forward == reverse
    # base fails t2,t4 that candidate passes -> cand_only_pass == 2.
    assert forward.cand_only_pass == 2
    assert forward.base_only_pass == 0
    assert forward.both_pass == 2
    assert forward.both_fail == 0
    assert forward.n == 4


def test_paired_table_rejects_mismatched_task_sets() -> None:
    with pytest.raises(ValueError, match="task"):
        PairedTable.from_outcomes({"t1": True}, {"t2": True})


# ---------------------------------------------------------------------------
# classify: the pure verdict function
# ---------------------------------------------------------------------------


def _uniform_outcomes(base_passes: int, cand_passes: int, n: int) -> PairedTable:
    """Build a table that is a pure function of arm pass counts.

    The first ``base_passes`` tasks pass under baseline; the first
    ``cand_passes`` tasks pass under candidate. This makes the discordant
    structure deterministic for fixtures.
    """
    base = {f"t{i:03d}": (i < base_passes) for i in range(n)}
    cand = {f"t{i:03d}": (i < cand_passes) for i in range(n)}
    return PairedTable.from_outcomes(base, cand)


def test_classify_significant_improvement_promotes() -> None:
    # Baseline passes 4/16, candidate passes 12/16, all baseline passes kept.
    table = _uniform_outcomes(base_passes=4, cand_passes=12, n=16)
    res = classify(table, alpha=DEFAULT_ALPHA, non_inferiority_margin=0.05, min_n=DEFAULT_MIN_N)
    assert res.verdict == Verdict.SIGNIFICANT_IMPROVEMENT
    assert res.min_n_satisfied is True


def test_classify_significant_regression() -> None:
    table = _uniform_outcomes(base_passes=14, cand_passes=4, n=16)
    res = classify(table, alpha=DEFAULT_ALPHA, non_inferiority_margin=0.05, min_n=DEFAULT_MIN_N)
    assert res.verdict == Verdict.SIGNIFICANT_REGRESSION


def test_classify_candidate_equals_baseline_is_insufficient() -> None:
    # Identical result sets: no discordant evidence -> insufficient_evidence.
    table = _uniform_outcomes(base_passes=10, cand_passes=10, n=16)
    res = classify(table, alpha=DEFAULT_ALPHA, non_inferiority_margin=0.05, min_n=DEFAULT_MIN_N)
    assert res.verdict == Verdict.INSUFFICIENT_EVIDENCE
    assert res.never_promotes is True


def test_classify_below_min_n_refuses_promotion_with_reason() -> None:
    # A strong effect but only n=4 tasks: an ordinary threshold gate would pass.
    table = _uniform_outcomes(base_passes=0, cand_passes=4, n=4)
    res = classify(table, alpha=DEFAULT_ALPHA, non_inferiority_margin=0.05, min_n=DEFAULT_MIN_N)
    assert res.verdict == Verdict.INSUFFICIENT_EVIDENCE
    assert res.reason == "below_minimum_n"
    assert res.min_n_satisfied is False
    assert res.never_promotes is True


def test_classify_noise_does_not_promote() -> None:
    # One extra pass out of 16 tasks: not significant, interval too wide.
    table = _uniform_outcomes(base_passes=8, cand_passes=9, n=16)
    res = classify(table, alpha=DEFAULT_ALPHA, non_inferiority_margin=0.05, min_n=DEFAULT_MIN_N)
    assert res.verdict != Verdict.SIGNIFICANT_IMPROVEMENT
    assert res.never_promotes is True


def test_classify_is_pure_and_reproducible() -> None:
    table = _uniform_outcomes(base_passes=4, cand_passes=12, n=16)
    a = classify(table, alpha=DEFAULT_ALPHA, non_inferiority_margin=0.05, min_n=DEFAULT_MIN_N)
    b = classify(table, alpha=DEFAULT_ALPHA, non_inferiority_margin=0.05, min_n=DEFAULT_MIN_N)
    assert a.to_dict() == b.to_dict()


def test_classify_rejects_unsupported_alpha() -> None:
    table = _uniform_outcomes(base_passes=4, cand_passes=12, n=16)
    with pytest.raises(ValueError, match="alpha"):
        classify(table, alpha=0.123, non_inferiority_margin=0.05, min_n=DEFAULT_MIN_N)


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def test_result_set_hash_is_order_invariant() -> None:
    a = result_set_hash({"t1": True, "t2": False})
    b = result_set_hash({"t2": False, "t1": True})
    assert a == b
    assert a.startswith("sha256:")


def test_suite_content_hash_is_order_invariant() -> None:
    a = suite_content_hash(["t2", "t1", "t3"])
    b = suite_content_hash(["t1", "t2", "t3"])
    assert a == b
