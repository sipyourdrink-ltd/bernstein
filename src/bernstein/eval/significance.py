"""Exact statistics for eval gate verdicts (#2520).

Every eval gate in the codebase compares point estimates over suites of a
handful of tasks, where a pass or fail verdict is frequently noise: a re-run
can flip it. This module makes the verdict a pure, dependency-free function of
the evidence, so the same two result sets always produce the same verdict on
any machine.

It provides:

* :func:`binom_tail_ge` - the exact upper tail of a fair-coin binomial, in
  exact rational arithmetic (:class:`fractions.Fraction`), so the McNemar
  paired test is bit-stable and never touches a float until the canonically
  rounded receipt value is produced.
* :func:`wilson_interval` - the Wilson score interval for a pass rate, using
  a fixed z-table (no SciPy) and only the correctly-rounded ``math.sqrt``.
* :class:`PairedTable` - the 2x2 discordance table over a paired suite; the
  sufficient statistic the verdict is re-derived from.
* :func:`classify` - the pure verdict function. One of
  :class:`Verdict`. A promoting verdict is refused below the minimum n per arm
  with an explicit machine-readable reason.

The design goal is that the verdict is *entailed* by the evidence: a verifier
holding only the 2x2 table, alpha, the non-inferiority margin, and the minimum
n recomputes the identical verdict, effect, interval, and p-values. Strip the
statistics and a gate degrades to a threshold compare over noise.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "CANON_PLACES",
    "DEFAULT_ALPHA",
    "DEFAULT_MIN_N",
    "DEFAULT_NON_INFERIORITY_MARGIN",
    "SUPPORTED_ALPHAS",
    "PairedTable",
    "SignificanceResult",
    "Verdict",
    "binom_tail_ge",
    "canonical_round",
    "classify",
    "result_set_hash",
    "suite_content_hash",
    "wilson_interval",
]


#: Number of decimal places every stored float is canonically rounded to.
#: Ten places is far below float64's ~15-17 significant digits, so the
#: round-half-even result is the nearest float to a clean decimal and its
#: shortest repr is stable across platforms.
CANON_PLACES: Final[int] = 10

#: Default two-sided significance level.
DEFAULT_ALPHA: Final[float] = 0.05

#: Default minimum n per arm before a *promoting* verdict may be returned.
#: Below this a strong-looking effect is treated as insufficient evidence.
DEFAULT_MIN_N: Final[int] = 12

#: Default non-inferiority margin on the paired difference of pass rates.
DEFAULT_NON_INFERIORITY_MARGIN: Final[float] = 0.05

#: Fixed two-sided z critical values keyed by alpha. Hard-coded so the module
#: needs no SciPy and stays deterministic; only these alphas are supported.
SUPPORTED_ALPHAS: Final[dict[float, float]] = {
    0.10: 1.6448536269514722,
    0.05: 1.959963984540054,
    0.01: 2.5758293035489004,
}

_CANON_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-CANON_PLACES)


class Verdict(StrEnum):
    """The four verdict strengths a gate decision can carry."""

    SIGNIFICANT_IMPROVEMENT = "significant_improvement"
    NON_INFERIOR = "non_inferior"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    SIGNIFICANT_REGRESSION = "significant_regression"


#: Verdicts that promote a candidate up the ladder.
PROMOTING_VERDICTS: Final[frozenset[Verdict]] = frozenset({Verdict.SIGNIFICANT_IMPROVEMENT, Verdict.NON_INFERIOR})


# ---------------------------------------------------------------------------
# Canonical rounding
# ---------------------------------------------------------------------------


def canonical_round(value: float, places: int = CANON_PLACES) -> float:
    """Round ``value`` to ``places`` decimals with round-half-to-even.

    The input float is deterministic (exact rationals plus the
    correctly-rounded :func:`math.sqrt`), so quantising its exact decimal
    value with a fixed rounding mode yields a bit-stable result across
    platforms.
    """
    if math.isnan(value) or math.isinf(value):
        msg = f"cannot canonically round non-finite value {value!r}"
        raise ValueError(msg)
    quantum = _CANON_QUANTUM if places == CANON_PLACES else Decimal(1).scaleb(-places)
    quantised = Decimal(value).quantize(quantum, rounding=ROUND_HALF_EVEN)
    # Normalise negative zero so ``-0.0`` never reaches a receipt.
    #
    # Adding positive zero is the sign fix, not a tolerance comparison: under
    # IEEE-754 round-to-nearest ``(-0.0) + 0.0`` is ``+0.0``, while for every
    # other finite value ``x + 0.0`` is exactly ``x`` (the addition is exact,
    # so no bit changes). A magnitude test such as :func:`math.isclose` would
    # be wrong here -- it would flatten genuinely small quantised p-values to
    # zero and change the statistical result.
    return float(quantised) + 0.0


# ---------------------------------------------------------------------------
# Exact binomial tail
# ---------------------------------------------------------------------------


def binom_tail_ge(k: int, n: int) -> Fraction:
    """Return ``P(X >= k)`` for ``X ~ Binomial(n, 1/2)`` as an exact rational.

    This is the exact one-sided p-value used by the McNemar paired test over
    ``b + c`` discordant pairs. The sum of binomial coefficients is exact
    integer arithmetic, so the value is identical on any platform.

    Args:
        k: Lower inclusive count. Values ``<= 0`` return the full mass.
        n: Number of trials.

    Raises:
        ValueError: If ``k`` or ``n`` is negative.
    """
    if k < 0:
        msg = f"k must be non-negative, got {k}"
        raise ValueError(msg)
    if n < 0:
        msg = f"n must be non-negative, got {n}"
        raise ValueError(msg)
    if k <= 0:
        return Fraction(1, 1)
    if k > n:
        return Fraction(0, 1)
    numerator = sum(math.comb(n, i) for i in range(k, n + 1))
    return Fraction(numerator, 1 << n)


# ---------------------------------------------------------------------------
# Wilson score interval
# ---------------------------------------------------------------------------


def wilson_interval(k: int, n: int, z: float) -> tuple[float, float]:
    """Return the Wilson score interval for ``k`` successes in ``n`` trials.

    Args:
        k: Number of successes.
        n: Number of trials.
        z: The two-sided z critical value (see :data:`SUPPORTED_ALPHAS`).

    Returns:
        ``(low, high)`` clamped to ``[0, 1]``. ``n == 0`` returns
        ``(0.0, 1.0)`` (no information).
    """
    if n <= 0:
        return (0.0, 1.0)
    p_hat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p_hat + z2 / (2.0 * n)) / denom
    margin = (z * math.sqrt((p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n)) / denom
    low = max(0.0, centre - margin)
    high = min(1.0, centre + margin)
    return (low, high)


def _paired_difference_interval(both_pass: int, b: int, c: int, both_fail: int, z: float) -> tuple[float, float]:
    """Return the confidence interval on the paired difference ``(c - b)/n``.

    Uses the standard McNemar normal-approximation interval on the difference
    of correlated proportions. All arithmetic is deterministic (the only
    transcendental is the correctly-rounded ``math.sqrt``).
    """
    n = both_pass + b + c + both_fail
    if n <= 0:
        return (0.0, 0.0)
    diff = (c - b) / n
    variance = (b + c - ((c - b) ** 2) / n) / (n * n)
    variance = max(variance, 0.0)
    margin = z * math.sqrt(variance)
    low = max(-1.0, diff - margin)
    high = min(1.0, diff + margin)
    return (low, high)


# ---------------------------------------------------------------------------
# Paired 2x2 discordance table
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PairedTable:
    """The 2x2 paired discordance table over a suite run under two arms.

    Attributes:
        both_pass: Tasks that pass under both baseline and candidate (n11).
        base_only_pass: Tasks that pass under baseline but fail under candidate
            (n10, the McNemar ``b``).
        cand_only_pass: Tasks that fail under baseline but pass under candidate
            (n01, the McNemar ``c``).
        both_fail: Tasks that fail under both (n00).
    """

    both_pass: int
    base_only_pass: int
    cand_only_pass: int
    both_fail: int

    def __post_init__(self) -> None:
        for name in ("both_pass", "base_only_pass", "cand_only_pass", "both_fail"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                msg = f"{name} must be a non-negative int, got {value!r}"
                raise ValueError(msg)

    @property
    def n(self) -> int:
        """Total paired sample size (equal per arm)."""
        return self.both_pass + self.base_only_pass + self.cand_only_pass + self.both_fail

    @property
    def base_pass(self) -> int:
        """Number of tasks the baseline passed."""
        return self.both_pass + self.base_only_pass

    @property
    def cand_pass(self) -> int:
        """Number of tasks the candidate passed."""
        return self.both_pass + self.cand_only_pass

    @property
    def base_rate(self) -> float:
        return self.base_pass / self.n if self.n else 0.0

    @property
    def cand_rate(self) -> float:
        return self.cand_pass / self.n if self.n else 0.0

    @property
    def discordant(self) -> int:
        """Number of discordant pairs (``b + c``); the McNemar evidence."""
        return self.base_only_pass + self.cand_only_pass

    def to_dict(self) -> dict[str, int]:
        return {
            "both_pass": self.both_pass,
            "base_only_pass": self.base_only_pass,
            "cand_only_pass": self.cand_only_pass,
            "both_fail": self.both_fail,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PairedTable:
        return cls(
            both_pass=int(raw["both_pass"]),
            base_only_pass=int(raw["base_only_pass"]),
            cand_only_pass=int(raw["cand_only_pass"]),
            both_fail=int(raw["both_fail"]),
        )

    @classmethod
    def from_outcomes(cls, baseline: Mapping[str, bool], candidate: Mapping[str, bool]) -> PairedTable:
        """Build the table from paired per-task pass/fail outcomes.

        Both mappings must cover the identical task-id set (a paired suite).
        Iteration is over the sorted task ids, so the table is invariant to the
        ingestion order of either mapping.

        Raises:
            ValueError: If the two task-id sets differ.
        """
        base_keys = set(baseline)
        cand_keys = set(candidate)
        if base_keys != cand_keys:
            missing = sorted(base_keys ^ cand_keys)
            msg = f"baseline and candidate must cover the same task set; differing task ids: {missing}"
            raise ValueError(msg)
        both_pass = base_only = cand_only = both_fail = 0
        for task_id in sorted(base_keys):
            bp = baseline[task_id]
            cp = candidate[task_id]
            if bp and cp:
                both_pass += 1
            elif bp and not cp:
                base_only += 1
            elif (not bp) and cp:
                cand_only += 1
            else:
                both_fail += 1
        return cls(both_pass=both_pass, base_only_pass=base_only, cand_only_pass=cand_only, both_fail=both_fail)


# ---------------------------------------------------------------------------
# Significance result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignificanceResult:
    """The re-derivable verdict plus every quantity it was decided on.

    Every float field is canonically rounded so the result is byte-stable.
    ``verdict`` is entailed by ``table`` together with ``alpha``,
    ``non_inferiority_margin``, and ``min_n``: :func:`classify` recomputes an
    identical result from those alone.
    """

    verdict: Verdict
    reason: str
    test: str
    alpha: float
    n_baseline: int
    n_candidate: int
    base_pass: int
    cand_pass: int
    base_rate: float
    cand_rate: float
    effect: float
    interval_low: float
    interval_high: float
    p_improvement: float
    p_regression: float
    min_n: int
    min_n_satisfied: bool
    non_inferiority_margin: float
    table: PairedTable

    @property
    def never_promotes(self) -> bool:
        """True when this verdict cannot lift a candidate up the ladder."""
        return self.verdict not in PROMOTING_VERDICTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "test": self.test,
            "alpha": self.alpha,
            "n_baseline": self.n_baseline,
            "n_candidate": self.n_candidate,
            "base_pass": self.base_pass,
            "cand_pass": self.cand_pass,
            "base_rate": self.base_rate,
            "cand_rate": self.cand_rate,
            "effect": self.effect,
            "interval_low": self.interval_low,
            "interval_high": self.interval_high,
            "p_improvement": self.p_improvement,
            "p_regression": self.p_regression,
            "min_n": self.min_n,
            "min_n_satisfied": self.min_n_satisfied,
            "non_inferiority_margin": self.non_inferiority_margin,
            "table": self.table.to_dict(),
        }


def _alpha_fraction(alpha: float) -> Fraction:
    """Return ``alpha`` as an exact rational (e.g. 0.05 -> 1/20)."""
    return Fraction(str(alpha))


def _decide(
    *,
    table: PairedTable,
    p_improve: Fraction,
    p_regress: Fraction,
    interval_low: float,
    alpha: float,
    margin: float,
    min_n: int,
) -> tuple[Verdict, str, bool]:
    """Return ``(verdict, reason, min_n_satisfied)`` from the evidence.

    The order matters: a significant regression is reported even below the
    minimum n (it never promotes), while an improvement or non-inferiority is
    downgraded to insufficient evidence when the sample is too small.
    """
    b = table.base_only_pass
    c = table.cand_only_pass
    n = table.n
    min_n_satisfied = n >= min_n
    alpha_q = _alpha_fraction(alpha)

    if n == 0:
        return (Verdict.INSUFFICIENT_EVIDENCE, "empty_suite", False)

    # A significant regression is admissible at any n: it only blocks.
    if b > c and p_regress <= alpha_q:
        return (Verdict.SIGNIFICANT_REGRESSION, "significant_regression", min_n_satisfied)

    if not min_n_satisfied:
        return (Verdict.INSUFFICIENT_EVIDENCE, "below_minimum_n", False)

    if c > b and p_improve <= alpha_q:
        return (Verdict.SIGNIFICANT_IMPROVEMENT, "significant_improvement", True)

    # Non-inferiority requires actual discordant evidence and an interval that
    # rules out a regression worse than the margin. Identical result sets carry
    # no discordant evidence and therefore stay insufficient.
    if table.discordant > 0 and interval_low >= -margin:
        return (Verdict.NON_INFERIOR, "non_inferior", True)

    return (Verdict.INSUFFICIENT_EVIDENCE, "no_significant_effect", True)


def classify(
    table: PairedTable,
    *,
    alpha: float = DEFAULT_ALPHA,
    non_inferiority_margin: float = DEFAULT_NON_INFERIORITY_MARGIN,
    min_n: int = DEFAULT_MIN_N,
) -> SignificanceResult:
    """Return the verdict entailed by ``table`` under the given parameters.

    Args:
        table: The paired 2x2 discordance table.
        alpha: Significance level; must be one of :data:`SUPPORTED_ALPHAS`.
        non_inferiority_margin: Largest tolerated regression on the paired
            difference before non-inferiority is refused.
        min_n: Minimum n per arm before a promoting verdict is allowed.

    Returns:
        A :class:`SignificanceResult` whose every derived field is a pure,
        canonically rounded function of the inputs.

    Raises:
        ValueError: If ``alpha`` is not supported or ``min_n`` is negative.
    """
    if alpha not in SUPPORTED_ALPHAS:
        supported = ", ".join(str(a) for a in sorted(SUPPORTED_ALPHAS))
        msg = f"unsupported alpha {alpha!r}; supported: {supported}"
        raise ValueError(msg)
    if min_n < 0:
        msg = f"min_n must be non-negative, got {min_n}"
        raise ValueError(msg)

    z = SUPPORTED_ALPHAS[alpha]
    b = table.base_only_pass
    c = table.cand_only_pass
    discordant = b + c

    p_improve = binom_tail_ge(c, discordant)
    p_regress = binom_tail_ge(b, discordant)
    interval_low, interval_high = _paired_difference_interval(table.both_pass, b, c, table.both_fail, z)
    effect = table.cand_rate - table.base_rate

    verdict, reason, min_n_satisfied = _decide(
        table=table,
        p_improve=p_improve,
        p_regress=p_regress,
        interval_low=interval_low,
        alpha=alpha,
        margin=non_inferiority_margin,
        min_n=min_n,
    )

    return SignificanceResult(
        verdict=verdict,
        reason=reason,
        test="mcnemar_exact",
        alpha=alpha,
        n_baseline=table.n,
        n_candidate=table.n,
        base_pass=table.base_pass,
        cand_pass=table.cand_pass,
        base_rate=canonical_round(table.base_rate),
        cand_rate=canonical_round(table.cand_rate),
        effect=canonical_round(effect),
        interval_low=canonical_round(interval_low),
        interval_high=canonical_round(interval_high),
        p_improvement=canonical_round(float(p_improve)),
        p_regression=canonical_round(float(p_regress)),
        min_n=min_n,
        min_n_satisfied=min_n_satisfied,
        non_inferiority_margin=non_inferiority_margin,
        table=table,
    )


# ---------------------------------------------------------------------------
# Content-addressed hashing of result sets and suites
# ---------------------------------------------------------------------------


def _hash_obj(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def result_set_hash(outcomes: Mapping[str, bool]) -> str:
    """Return an order-invariant content hash over per-task pass/fail outcomes."""
    canonical = dict(outcomes)
    return _hash_obj(canonical)


def suite_content_hash(task_ids: Sequence[str]) -> str:
    """Return an order-invariant content hash over a suite's task ids."""
    return _hash_obj(sorted(task_ids))
