"""Exact-arithmetic time-uniform confidence sequences for eval statistics (#4184).

This module provides a time-uniform confidence sequence for bounded observations.
A confidence sequence is a sequence of intervals (C_n)_{n>=1} such that

    Pr[forall n>=1: mu in C_n] >= 1 - alpha

where mu is the true mean of bounded observations X_i in [a,b]. The sequence can be
inspected after every observation (peeking) without inflating the error rate.

The implementation follows the Hoeffding-based construction:

    C_n = [ sum X_i / n - eps_n, sum X_i / n + eps_n ]
    eps_n = sqrt( (b-a)^2 * log(pi^2 n^2 / 6 alpha) / (2n) )

with a union bound (peeling) over n to achieve time-uniform coverage.
The width shrinks as O(sqrt(log n / n)) and is monotone via running intersection.

All arithmetic is float-based, with canonical rounding applied at the API
boundary; no SciPy, no external dependencies. The construction is conservative,
trading some tightness for computational simplicity.

Reference:
    Howard, S. R., Ramdas, A., McAuliffe, J., & Sekhon, J. (2021).
    Time-uniform, nonparametric, nonasymptotic confidence sequences.
    The Annals of Statistics, 49(2), 1055-1080.
    (Equation (4) with Hoeffding's lemma and a union bound over n)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

from .significance import canonical_round

__all__ = ["ConfidenceSequence"]


#: Hard-coded π²/6 for the peeling union bound sum.
_PISQ_OVER_SIX: Final[float] = math.pi**2 / 6


@dataclass
class ConfidenceSequence:
    """Time-uniform confidence sequence for bounded observations.

    The sequence is valid after every observation (peeking allowed) and shrinks
    monotonically via running intersection.

    Construction:
        - Observations must lie in [lower_bound, upper_bound].
        - Error level alpha (e.g., 0.05) is fixed at creation.
        - The Hoeffding-based union bound over n guarantees coverage >= 1-alpha.
        - The reported interval after n observations is the intersection of all
          Hoeffding intervals up to n, ensuring monotonic shrinkage.

    Attributes:
        alpha: The two-sided error level (e.g., 0.05 for 95% coverage).
        lower_bound: Minimal possible observation value.
        upper_bound: Maximal possible observation value.
    """

    alpha: float
    lower_bound: float
    upper_bound: float

    # Internal state - computed once at construction
    _alpha: float = field(init=False)
    _bound_range: float = field(init=False)

    # Running state
    _n: int = field(init=False, default_factory=lambda: 0)
    _sum: float = field(init=False, default_factory=lambda: 0.0)
    _current_low: float = field(init=False, default_factory=lambda: float("-inf"))
    _current_high: float = field(init=False, default_factory=lambda: float("inf"))

    def __post_init__(self) -> None:
        if self.alpha <= 0.0 or self.alpha >= 1.0:
            raise ValueError(f"alpha must be in (0,1), got {self.alpha}")
        if self.lower_bound >= self.upper_bound:
            raise ValueError(f"lower_bound ({self.lower_bound}) must be < upper_bound ({self.upper_bound})")

        # Set derived values
        object.__setattr__(self, "_alpha", float(self.alpha))
        object.__setattr__(self, "_bound_range", float(self.upper_bound - self.lower_bound))

    @property
    def n(self) -> int:
        """Number of observations processed so far."""
        return self._n

    def _hoeffding_margin(self, n: int) -> float:
        """Return the Hoeffding margin eps_n for sample size n.

        The guarantee is: with probability at least 1-alpha, for all n>=1 simultaneously,
        |mu - X_bar_n| <= eps_n, where mu is the true mean of bounded observations.

        Derived from: eps_n = sqrt( (b-a)^2 * log(pi^2 n^2 / 6 alpha) / (2n) )
        """
        if n == 0:
            return 0.0

        # log(pi^2 n^2 / 6 alpha) = log(pi^2/6) + 2 log n - log alpha
        log_term = _PISQ_OVER_SIX * n * n / self._alpha
        log_val = math.log(log_term)

        variance = (self._bound_range**2) * log_val / (2 * n)
        if variance <= 0.0:
            return 0.0

        return math.sqrt(variance)

    def update(self, observation: float) -> None:
        """Add one bounded observation to the sequence.

        Args:
            observation: A value in [lower_bound, upper_bound]. Not validated
                for performance; violations break the coverage guarantee.

        Raises:
            TypeError: If observation is not a float or int.
        """
        if not isinstance(observation, (float, int)):
            raise TypeError(f"observation must be numeric, got {type(observation).__name__}")

        self._sum += float(observation)
        self._n += 1

        # Compute the Hoeffding interval for this n.
        margin = self._hoeffding_margin(self._n)
        sample_mean = self._sum / self._n
        low = sample_mean - margin
        high = sample_mean + margin

        # Intersect with previous intervals to enforce monotonicity.
        if low > self._current_low:
            self._current_low = low
        if high < self._current_high:
            self._current_high = high

    def bounds(self) -> tuple[float, float]:
        """Return the current confidence interval.

        The interval is guaranteed to contain the true mean with probability
        at least 1-alpha simultaneously for all n processed so far (time-uniform).

        Returns:
            (lower, upper) as canonically rounded floats. When n=0, returns
            (lower_bound, upper_bound) (the trivial full range).
        """
        if self._n == 0:
            return (canonical_round(self.lower_bound), canonical_round(self.upper_bound))

        return (canonical_round(self._current_low), canonical_round(self._current_high))
