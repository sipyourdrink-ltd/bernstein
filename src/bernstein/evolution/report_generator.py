"""Analysis result types, statistical helpers, and Goodhart's Law defenses."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Analysis result types
# ---------------------------------------------------------------------------


@dataclass
class TrendAnalysis:
    """Results of trend analysis."""

    metric_name: str
    direction: Literal["increasing", "decreasing", "stable"]
    change_percent: float
    baseline_value: float
    current_value: float
    confidence: float
    period_days: int = 7
    mann_kendall_p: float | None = None


@dataclass
class AnomalyDetection:
    """Detected anomaly in metrics."""

    metric_name: str
    anomaly_type: Literal["spike", "drop", "outlier"]
    severity: Literal["low", "medium", "high", "critical"]
    z_score: float
    expected_value: float
    actual_value: float
    timestamp: float
    description: str


@dataclass
class EWMAState:
    """State for Exponential Weighted Moving Average tracking."""

    metric_name: str
    lambda_: float
    current_value: float = 0.0
    ucl: float = 0.0
    lcl: float = 0.0
    center_line: float = 0.0
    n_observations: int = 0
    in_control: bool = True


@dataclass
class CUSUMState:
    """State for CUSUM (Cumulative Sum) shift detection."""

    metric_name: str
    target: float = 0.0
    k: float = 0.5
    h: float = 4.0
    s_high: float = 0.0
    s_low: float = 0.0
    shift_detected: bool = False
    shift_direction: Literal["up", "down", "none"] = "none"
    n_observations: int = 0


@dataclass
class Changepoint:
    """Detected changepoint from BOCPD."""

    index: int
    probability: float
    run_length: int


@dataclass
class BetaBinomialPosterior:
    """Rolling Beta-Binomial posterior for pass/fail metrics."""

    metric_name: str
    alpha: float = 1.0
    beta: float = 1.0
    n_observations: int = 0

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        a, b = self.alpha, self.beta
        return (a * b) / ((a + b) ** 2 * (a + b + 1))

    @property
    def ci_95(self) -> tuple[float, float]:
        """Approximate 95% credible interval."""
        std = self.variance**0.5
        return (max(0.0, self.mean - 1.96 * std), min(1.0, self.mean + 1.96 * std))


@dataclass
class NormalInverseGammaPosterior:
    """Rolling Normal-Inverse-Gamma posterior for continuous metrics."""

    metric_name: str
    mu: float = 0.0
    kappa: float = 1.0
    alpha: float = 1.0
    beta: float = 1.0
    n_observations: int = 0

    @property
    def mean(self) -> float:
        return self.mu

    @property
    def variance(self) -> float:
        if self.alpha <= 1:
            return float("inf")
        return self.beta / (self.kappa * (self.alpha - 1))


@dataclass
class CompositeScore:
    """Multi-metric composite score (Goodhart defense)."""

    score: float
    components: dict[str, float]
    divergence_flags: list[str] = field(default_factory=list[str])
    trip_wire_flags: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------
# Minimum sample sizes
# ---------------------------------------------------------------------------

MIN_SAMPLES_ALERTING = 30
MIN_SAMPLES_AB = 50
MIN_SAMPLES_TREND = 200
MIN_SAMPLES_EWMA = 5
MIN_SAMPLES_CUSUM = 8
MIN_SAMPLES_MANN_KENDALL = 8
MIN_SAMPLES_BOCPD = 20


# ---------------------------------------------------------------------------
# Statistical helper functions
# ---------------------------------------------------------------------------


def _mann_kendall(values: list[float]) -> tuple[float, float]:
    """Mann-Kendall trend test. Returns (S statistic, two-sided p-value).

    Valid from n=8. Uses normal approximation for p-value.
    """
    n = len(values)
    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            diff = values[j] - values[i]
            if diff > 0:
                s += 1
            elif diff < 0:
                s -= 1

    var_s = n * (n - 1) * (2 * n + 5) / 18.0
    if var_s == 0:
        return 0.0, 1.0

    if s > 0:
        z = (s - 1) / var_s**0.5
    elif s < 0:
        z = (s + 1) / var_s**0.5
    else:
        z = 0.0

    p = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return float(s), p


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _ewma_update(prev: float, new_value: float, lambda_: float) -> float:
    """Single EWMA update: Z_t = lambda * X_t + (1 - lambda) * Z_{t-1}."""
    return lambda_ * new_value + (1.0 - lambda_) * prev


def _ewma_control_limits(
    sigma: float,
    lambda_: float,
    n: int,
    big_l: float = 3.0,
) -> tuple[float, float]:
    """Compute EWMA control limits."""
    ratio = lambda_ / (2.0 - lambda_)
    correction = 1.0 - (1.0 - lambda_) ** (2 * n)
    width = big_l * sigma * (ratio * correction) ** 0.5
    return width, -width


def _cusum_update(
    value: float,
    target: float,
    k: float,
    s_high: float,
    s_low: float,
) -> tuple[float, float]:
    """Update CUSUM accumulators."""
    s_high_new = max(0.0, s_high + (value - target) - k)
    s_low_new = max(0.0, s_low - (value - target) - k)
    return s_high_new, s_low_new


def _bocpd_offline(
    values: list[float],
    hazard_rate: float = 1.0 / 250.0,
) -> list[Changepoint]:
    """Bayesian Online Changepoint Detection (Adams & MacKay 2007).

    Simplified implementation using normal-normal conjugate model.
    Returns detected changepoints with probability > 0.5.
    """
    n = len(values)
    if n < 2:
        return []

    max_run = n + 1
    mu0 = sum(values) / n
    kappa0 = 1.0
    alpha0 = 1.0
    beta0 = 1.0

    run_length_probs = [0.0] * max_run
    run_length_probs[0] = 1.0

    mu_params = [mu0] * max_run
    kappa_params = [kappa0] * max_run
    alpha_params = [alpha0] * max_run
    beta_params = [beta0] * max_run

    changepoints: list[Changepoint] = []

    for t in range(n):
        x = values[t]

        pred_probs = [0.0] * max_run
        for r in range(min(t + 1, max_run)):
            mu_r = mu_params[r]
            kappa_r = kappa_params[r]
            alpha_r = alpha_params[r]
            beta_r = beta_params[r]

            pred_var = beta_r * (kappa_r + 1) / (alpha_r * kappa_r)
            if pred_var <= 0:
                pred_var = 1e-10
            pred_probs[r] = _student_t_pdf(x, mu_r, pred_var, 2 * alpha_r)

        new_run_probs = [0.0] * max_run
        for r in range(min(t + 1, max_run - 1)):
            new_run_probs[r + 1] = run_length_probs[r] * pred_probs[r] * (1 - hazard_rate)

        cp_mass = sum(run_length_probs[r] * pred_probs[r] * hazard_rate for r in range(min(t + 1, max_run)))
        new_run_probs[0] = cp_mass

        total = sum(new_run_probs)
        if total > 0:
            for r in range(max_run):
                new_run_probs[r] /= total

        if t > 0 and new_run_probs[0] > 0.5:
            changepoints.append(
                Changepoint(
                    index=t,
                    probability=new_run_probs[0],
                    run_length=0,
                )
            )

        new_mu = [mu0] * max_run
        new_kappa = [kappa0] * max_run
        new_alpha = [alpha0] * max_run
        new_beta = [beta0] * max_run

        for r in range(min(t + 1, max_run - 1)):
            k_r = kappa_params[r]
            m_r = mu_params[r]
            a_r = alpha_params[r]
            b_r = beta_params[r]

            new_kappa[r + 1] = k_r + 1
            new_mu[r + 1] = (k_r * m_r + x) / (k_r + 1)
            new_alpha[r + 1] = a_r + 0.5
            new_beta[r + 1] = b_r + k_r * (x - m_r) ** 2 / (2 * (k_r + 1))

        run_length_probs = new_run_probs
        mu_params = new_mu
        kappa_params = new_kappa
        alpha_params = new_alpha
        beta_params = new_beta

    return changepoints


def _student_t_pdf(x: float, mu: float, var: float, nu: float) -> float:
    """Unnormalized Student-t PDF for BOCPD predictive likelihood."""
    if nu <= 0 or var <= 0:
        return 1e-10
    z = (x - mu) ** 2 / var
    log_p = (
        math.lgamma((nu + 1) / 2)
        - math.lgamma(nu / 2)
        - 0.5 * math.log(nu * math.pi * var)
        - ((nu + 1) / 2) * math.log(1 + z / nu)
    )
    return math.exp(log_p)


def _std(values: list[float]) -> float:
    """Population standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((x - mean) ** 2 for x in values) / len(values)) ** 0.5


# ---------------------------------------------------------------------------
# Goodhart's Law defenses
# ---------------------------------------------------------------------------

_COMPOSITE_WEIGHTS: dict[str, float] = {
    "success_rate": 0.30,
    "cost_efficiency": 0.20,
    "duration_efficiency": 0.20,
    "code_quality": 0.15,
    "retry_rate_inv": 0.15,
}

_CORRELATED_PAIRS: list[tuple[str, str]] = [
    ("success_rate", "code_quality"),
    ("duration_efficiency", "success_rate"),
    ("cost_efficiency", "code_quality"),
]
