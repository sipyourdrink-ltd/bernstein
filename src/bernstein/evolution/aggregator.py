"""Metrics aggregation with EWMA, CUSUM, BOCPD, and Goodhart defenses."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metric record types
# ---------------------------------------------------------------------------


@dataclass
class MetricRecord:
    """Base class for metric records."""

    timestamp: float
    task_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "role": self.role,
        }


@dataclass
class TaskMetrics(MetricRecord):
    """Metrics for a completed task."""

    model: str | None = None
    provider: str | None = None
    duration_seconds: float = 0.0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    cost_usd: float = 0.0
    janitor_passed: bool = True
    files_modified: int = 0
    lines_added: int = 0
    lines_deleted: int = 0

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "model": self.model,
                "provider": self.provider,
                "duration_seconds": self.duration_seconds,
                "tokens_prompt": self.tokens_prompt,
                "tokens_completion": self.tokens_completion,
                "cost_usd": self.cost_usd,
                "janitor_passed": self.janitor_passed,
                "files_modified": self.files_modified,
                "lines_added": self.lines_added,
                "lines_deleted": self.lines_deleted,
            }
        )
        return base


@dataclass
class AgentMetrics(MetricRecord):
    """Metrics for an agent session."""

    agent_id: str | None = None
    lifetime_seconds: float = 0.0
    tasks_completed: int = 0
    heartbeat_failures: int = 0
    sleep_incidents: int = 0
    context_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "agent_id": self.agent_id,
                "lifetime_seconds": self.lifetime_seconds,
                "tasks_completed": self.tasks_completed,
                "heartbeat_failures": self.heartbeat_failures,
                "sleep_incidents": self.sleep_incidents,
                "context_tokens": self.context_tokens,
            }
        )
        return base


@dataclass
class CostMetrics(MetricRecord):
    """Cost metrics for a provider."""

    provider: str | None = None
    model: str | None = None
    tier: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    rate_limit_remaining: int | None = None
    free_tier_remaining: int | None = None

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "provider": self.provider,
                "model": self.model,
                "tier": self.tier,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "cost_usd": self.cost_usd,
                "rate_limit_remaining": self.rate_limit_remaining,
                "free_tier_remaining": self.free_tier_remaining,
            }
        )
        return base


@dataclass
class QualityMetrics(MetricRecord):
    """Quality metrics."""

    janitor_pass_rate: float = 0.0
    human_approval_rate: float = 0.0
    rollback_rate: float = 0.0
    test_pass_rate: float = 0.0
    rework_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "janitor_pass_rate": self.janitor_pass_rate,
                "human_approval_rate": self.human_approval_rate,
                "rollback_rate": self.rollback_rate,
                "test_pass_rate": self.test_pass_rate,
                "rework_rate": self.rework_rate,
            }
        )
        return base


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
# Collector protocol & implementation
# ---------------------------------------------------------------------------


class MetricsCollector(Protocol):
    """Protocol for metrics collection."""

    def record_task_metrics(self, metrics: TaskMetrics) -> None: ...
    def record_agent_metrics(self, metrics: AgentMetrics) -> None: ...
    def record_cost_metrics(self, metrics: CostMetrics) -> None: ...
    def record_quality_metrics(self, metrics: QualityMetrics) -> None: ...
    def get_recent_task_metrics(self, hours: int = 24) -> list[TaskMetrics]: ...
    def get_recent_cost_metrics(self, hours: int = 24) -> list[CostMetrics]: ...


class FileMetricsCollector:
    """Collects and stores metrics to JSONL files in .sdd/metrics/."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.metrics_dir = state_dir / "metrics"
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

        self._task_metrics: list[TaskMetrics] = []
        self._agent_metrics: list[AgentMetrics] = []
        self._cost_metrics: list[CostMetrics] = []
        self._quality_metrics: list[QualityMetrics] = []

    def record_task_metrics(self, metrics: TaskMetrics) -> None:
        self._task_metrics.append(metrics)
        self._append_to_file("tasks.jsonl", metrics.to_dict())

    def record_agent_metrics(self, metrics: AgentMetrics) -> None:
        self._agent_metrics.append(metrics)
        self._append_to_file("agents.jsonl", metrics.to_dict())

    def record_cost_metrics(self, metrics: CostMetrics) -> None:
        self._cost_metrics.append(metrics)
        self._append_to_file("costs.jsonl", metrics.to_dict())

    def record_quality_metrics(self, metrics: QualityMetrics) -> None:
        self._quality_metrics.append(metrics)
        self._append_to_file("quality.jsonl", metrics.to_dict())

    def _append_to_file(self, filename: str, data: dict[str, Any]) -> None:
        filepath = self.metrics_dir / filename
        with filepath.open("a") as f:
            f.write(json.dumps(data) + "\n")

    def get_recent_task_metrics(self, hours: int = 24) -> list[TaskMetrics]:
        cutoff = time.time() - (hours * 3600)
        return [m for m in self._task_metrics if m.timestamp >= cutoff]

    def get_recent_cost_metrics(self, hours: int = 24) -> list[CostMetrics]:
        cutoff = time.time() - (hours * 3600)
        return [m for m in self._cost_metrics if m.timestamp >= cutoff]

    def get_recent_agent_metrics(self, hours: int = 24) -> list[AgentMetrics]:
        cutoff = time.time() - (hours * 3600)
        return [m for m in self._agent_metrics if m.timestamp >= cutoff]

    def get_recent_quality_metrics(self, hours: int = 24) -> list[QualityMetrics]:
        cutoff = time.time() - (hours * 3600)
        return [m for m in self._quality_metrics if m.timestamp >= cutoff]

    def load_from_files(self) -> None:
        self._task_metrics = self._load_from_file("tasks.jsonl", TaskMetrics)
        self._agent_metrics = self._load_from_file("agents.jsonl", AgentMetrics)
        self._cost_metrics = self._load_from_file("costs.jsonl", CostMetrics)
        self._quality_metrics = self._load_from_file("quality.jsonl", QualityMetrics)

    def _load_from_file(self, filename: str, cls: type[Any]) -> list[Any]:
        filepath = self.metrics_dir / filename
        if not filepath.exists():
            return []
        records: list[Any] = []
        with filepath.open() as f:
            for line in f:
                if line.strip():
                    data: dict[str, Any] = json.loads(line)
                    with contextlib.suppress(TypeError):
                        records.append(cls(**data))
        return records


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


def _bocpd_compute_predictive_probs(
    x: float,
    t: int,
    max_run: int,
    mu_params: list[float],
    kappa_params: list[float],
    alpha_params: list[float],
    beta_params: list[float],
) -> list[float]:
    """Compute predictive probabilities for each run length at time *t*."""
    pred_probs = [0.0] * max_run
    for r in range(min(t + 1, max_run)):
        pred_var = beta_params[r] * (kappa_params[r] + 1) / (alpha_params[r] * kappa_params[r])
        if pred_var <= 0:
            pred_var = 1e-10
        pred_probs[r] = _student_t_pdf(x, mu_params[r], pred_var, 2 * alpha_params[r])
    return pred_probs


def _bocpd_update_params(
    x: float,
    t: int,
    max_run: int,
    mu0: float,
    kappa0: float,
    alpha0: float,
    beta0: float,
    mu_params: list[float],
    kappa_params: list[float],
    alpha_params: list[float],
    beta_params: list[float],
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Update conjugate prior parameters after observing *x* at time *t*."""
    new_mu = [mu0] * max_run
    new_kappa = [kappa0] * max_run
    new_alpha = [alpha0] * max_run
    new_beta = [beta0] * max_run

    for r in range(min(t + 1, max_run - 1)):
        k_r = kappa_params[r]
        m_r = mu_params[r]
        new_kappa[r + 1] = k_r + 1
        new_mu[r + 1] = (k_r * m_r + x) / (k_r + 1)
        new_alpha[r + 1] = alpha_params[r] + 0.5
        new_beta[r + 1] = beta_params[r] + k_r * (x - m_r) ** 2 / (2 * (k_r + 1))

    return new_mu, new_kappa, new_alpha, new_beta


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

        pred_probs = _bocpd_compute_predictive_probs(
            x, t, max_run, mu_params, kappa_params, alpha_params, beta_params
        )

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
                Changepoint(index=t, probability=new_run_probs[0], run_length=0)
            )

        mu_params, kappa_params, alpha_params, beta_params = _bocpd_update_params(
            x, t, max_run, mu0, kappa0, alpha0, beta0,
            mu_params, kappa_params, alpha_params, beta_params,
        )

        run_length_probs = new_run_probs

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


# ---------------------------------------------------------------------------
# MetricsAggregator
# ---------------------------------------------------------------------------


class MetricsAggregator:
    """Aggregates metrics with EWMA, CUSUM, BOCPD, and Goodhart defenses.

    Statistical methods:
    - EWMA (lambda=0.2) for real-time control charts
    - CUSUM for shift detection (better than z-score for small n)
    - BOCPD (Adams & MacKay) every 20 records for changepoint detection
    - Mann-Kendall test for trend significance (valid from n=8)
    - Rolling Beta-Binomial posteriors for pass/fail metrics
    - Rolling Normal-Inverse-Gamma posteriors for continuous metrics

    Goodhart defenses:
    - Multi-metric composite scoring with hidden weights
    - Metric divergence detection between correlated pairs
    - Trip wire monitoring for gaming detection
    """

    def __init__(
        self,
        collector: MetricsCollector,
        analysis_dir: Path | None = None,
    ) -> None:
        self.collector = collector
        self._analysis_dir = analysis_dir
        self._ewma_states: dict[str, EWMAState] = {}
        self._cusum_states: dict[str, CUSUMState] = {}
        self._beta_posteriors: dict[str, BetaBinomialPosterior] = {}
        self._nig_posteriors: dict[str, NormalInverseGammaPosterior] = {}
        self._metric_history: dict[str, list[float]] = {}

    # -------------------------------------------------------------------
    # EWMA
    # -------------------------------------------------------------------

    def update_ewma(
        self,
        metric_name: str,
        value: float,
        lambda_: float = 0.2,
        sigma: float | None = None,
    ) -> EWMAState:
        """Update EWMA chart for a metric."""
        state = self._ewma_states.get(metric_name)
        if state is None:
            state = EWMAState(
                metric_name=metric_name,
                lambda_=lambda_,
                current_value=value,
                center_line=value,
                n_observations=1,
            )
            self._ewma_states[metric_name] = state
            return state

        state.n_observations += 1
        state.current_value = _ewma_update(state.current_value, value, lambda_)

        if sigma is not None and state.n_observations >= MIN_SAMPLES_EWMA:
            width, _ = _ewma_control_limits(sigma, lambda_, state.n_observations)
            state.ucl = state.center_line + width
            state.lcl = state.center_line - width
            state.in_control = state.lcl <= state.current_value <= state.ucl

        return state

    def get_ewma_state(self, metric_name: str) -> EWMAState | None:
        return self._ewma_states.get(metric_name)

    # -------------------------------------------------------------------
    # CUSUM
    # -------------------------------------------------------------------

    def update_cusum(
        self,
        metric_name: str,
        value: float,
        target: float | None = None,
        k: float = 0.5,
        h: float = 4.0,
    ) -> CUSUMState:
        """Update CUSUM chart for a metric."""
        state = self._cusum_states.get(metric_name)
        if state is None:
            t = target if target is not None else value
            state = CUSUMState(
                metric_name=metric_name,
                target=t,
                k=k,
                h=h,
                n_observations=1,
            )
            self._cusum_states[metric_name] = state
            return state

        if target is not None:
            state.target = target

        state.n_observations += 1
        state.s_high, state.s_low = _cusum_update(
            value,
            state.target,
            state.k,
            state.s_high,
            state.s_low,
        )

        if state.s_high > state.h:
            state.shift_detected = True
            state.shift_direction = "up"
        elif state.s_low > state.h:
            state.shift_detected = True
            state.shift_direction = "down"
        else:
            state.shift_detected = False
            state.shift_direction = "none"

        return state

    def get_cusum_state(self, metric_name: str) -> CUSUMState | None:
        return self._cusum_states.get(metric_name)

    def reset_cusum(self, metric_name: str) -> None:
        """Reset CUSUM accumulators after a detected shift is handled."""
        state = self._cusum_states.get(metric_name)
        if state is not None:
            state.s_high = 0.0
            state.s_low = 0.0
            state.shift_detected = False
            state.shift_direction = "none"

    # -------------------------------------------------------------------
    # BOCPD
    # -------------------------------------------------------------------

    def detect_changepoints(
        self,
        values: list[float],
        hazard_rate: float = 1.0 / 250.0,
    ) -> list[Changepoint]:
        """Run BOCPD on a sequence. Call every ~20 records."""
        if len(values) < MIN_SAMPLES_BOCPD:
            return []
        return _bocpd_offline(values, hazard_rate)

    # -------------------------------------------------------------------
    # Mann-Kendall
    # -------------------------------------------------------------------

    def mann_kendall_test(self, values: list[float]) -> tuple[float, float] | None:
        """Run Mann-Kendall trend test. Returns (S, p-value) or None if < 8 samples."""
        if len(values) < MIN_SAMPLES_MANN_KENDALL:
            return None
        return _mann_kendall(values)

    # -------------------------------------------------------------------
    # Bayesian posteriors
    # -------------------------------------------------------------------

    def update_beta_binomial(
        self,
        metric_name: str,
        success: bool,
    ) -> BetaBinomialPosterior:
        """Update rolling Beta-Binomial posterior for a pass/fail metric."""
        posterior = self._beta_posteriors.get(metric_name)
        if posterior is None:
            posterior = BetaBinomialPosterior(metric_name=metric_name)
            self._beta_posteriors[metric_name] = posterior

        posterior.n_observations += 1
        if success:
            posterior.alpha += 1.0
        else:
            posterior.beta += 1.0

        return posterior

    def get_beta_posterior(self, metric_name: str) -> BetaBinomialPosterior | None:
        return self._beta_posteriors.get(metric_name)

    def update_normal_inverse_gamma(
        self,
        metric_name: str,
        value: float,
    ) -> NormalInverseGammaPosterior:
        """Update rolling Normal-Inverse-Gamma posterior for a continuous metric."""
        posterior = self._nig_posteriors.get(metric_name)
        if posterior is None:
            posterior = NormalInverseGammaPosterior(metric_name=metric_name, mu=value)
            self._nig_posteriors[metric_name] = posterior
            posterior.n_observations = 1
            return posterior

        old_mu = posterior.mu
        old_kappa = posterior.kappa

        posterior.n_observations += 1
        posterior.kappa = old_kappa + 1
        posterior.mu = (old_kappa * old_mu + value) / posterior.kappa
        posterior.alpha = posterior.alpha + 0.5
        posterior.beta = posterior.beta + old_kappa * (value - old_mu) ** 2 / (2 * posterior.kappa)

        return posterior

    def get_nig_posterior(self, metric_name: str) -> NormalInverseGammaPosterior | None:
        return self._nig_posteriors.get(metric_name)

    # -------------------------------------------------------------------
    # Goodhart defenses
    # -------------------------------------------------------------------

    def _check_divergences(self, metrics: dict[str, float]) -> list[str]:
        """Detect correlated metric pairs that are moving in opposite directions."""
        flags: list[str] = []
        for m1, m2 in _CORRELATED_PAIRS:
            v1 = metrics.get(m1)
            v2 = metrics.get(m2)
            if v1 is None or v2 is None:
                continue
            h1 = self._metric_history.get(m1, [])
            h2 = self._metric_history.get(m2, [])
            if len(h1) < 3 or len(h2) < 3:
                continue
            d1 = v1 - sum(h1[-3:]) / 3
            d2 = v2 - sum(h2[-3:]) / 3
            if d1 > 0.05 and d2 < -0.05:
                flags.append(f"{m1} improving (+{d1:.2f}) while {m2} declining ({d2:.2f})")
            elif d2 > 0.05 and d1 < -0.05:
                flags.append(f"{m2} improving (+{d2:.2f}) while {m1} declining ({d1:.2f})")
        return flags

    def _check_trip_wires(self, metrics: dict[str, float]) -> list[str]:
        """Flag suspicious perfect-score streaks."""
        flags: list[str] = []
        success_rate = metrics.get("success_rate", 0.0)
        history = self._metric_history.get("success_rate", [])
        if success_rate >= 1.0 and len(history) >= 5 and all(v >= 1.0 for v in history[-5:]):
            flags.append("success_rate has been 100% for 5+ consecutive windows — possible test gaming")
        return flags

    def _update_metric_history(self, metrics: dict[str, float]) -> None:
        """Append new metric values and cap history length."""
        for name, value in metrics.items():
            if name not in self._metric_history:
                self._metric_history[name] = []
            self._metric_history[name].append(value)
            if len(self._metric_history[name]) > 50:
                self._metric_history[name] = self._metric_history[name][-50:]

    def compute_composite_score(self, metrics: dict[str, float]) -> CompositeScore:
        """Compute multi-metric composite score with hidden weights."""
        score = 0.0
        components: dict[str, float] = {}
        for name, weight in _COMPOSITE_WEIGHTS.items():
            value = metrics.get(name, 0.0)
            components[name] = value * weight
            score += value * weight

        divergence_flags = self._check_divergences(metrics)
        trip_wire_flags = self._check_trip_wires(metrics)
        self._update_metric_history(metrics)

        return CompositeScore(
            score=score,
            components=components,
            divergence_flags=divergence_flags,
            trip_wire_flags=trip_wire_flags,
        )

    # -------------------------------------------------------------------
    # Batch ingestion
    # -------------------------------------------------------------------

    def ingest_task_metrics(self, metrics: list[TaskMetrics]) -> None:
        """Process a batch of task metrics through EWMA, CUSUM, and posteriors."""
        costs = [m.cost_usd for m in metrics]
        durations = [m.duration_seconds for m in metrics]

        cost_sigma = _std(costs) if len(costs) >= 2 else None
        dur_sigma = _std(durations) if len(durations) >= 2 else None

        for m in metrics:
            self.update_ewma("cost", m.cost_usd, sigma=cost_sigma)
            self.update_ewma("duration", m.duration_seconds, sigma=dur_sigma)
            self.update_cusum("cost", m.cost_usd)
            self.update_cusum("duration", m.duration_seconds)
            self.update_beta_binomial("janitor_pass", m.janitor_passed)
            self.update_normal_inverse_gamma("cost", m.cost_usd)
            self.update_normal_inverse_gamma("duration", m.duration_seconds)

    # -------------------------------------------------------------------
    # Trend and anomaly analysis
    # -------------------------------------------------------------------

    def analyze_trends(self) -> list[TrendAnalysis]:
        """Analyze trends using Mann-Kendall and simple half-split."""
        task_metrics = self.collector.get_recent_task_metrics(hours=168)
        if len(task_metrics) < 10:
            return []

        trends: list[TrendAnalysis] = []
        extractors: list[tuple[str, Callable[[TaskMetrics], float]]] = [
            ("cost_per_task", lambda m: m.cost_usd),
            ("task_duration", lambda m: m.duration_seconds),
            ("success_rate", lambda m: 1.0 if m.janitor_passed else 0.0),
        ]
        for metric_name, extractor in extractors:
            values = [extractor(m) for m in task_metrics]
            trend = self.calculate_trend(values, metric_name)
            if trend:
                trends.append(trend)

        return trends

    def calculate_trend(
        self,
        values: list[float],
        metric_name: str,
    ) -> TrendAnalysis | None:
        if len(values) < 5:
            return None

        mid = len(values) // 2
        baseline = values[:mid]
        current = values[mid:]

        baseline_avg = sum(baseline) / len(baseline)
        current_avg = sum(current) / len(current)

        change_percent = 0.0 if baseline_avg == 0 else (current_avg - baseline_avg) / baseline_avg * 100

        if change_percent > 10:
            direction: Literal["increasing", "decreasing", "stable"] = "increasing"
        elif change_percent < -10:
            direction = "decreasing"
        else:
            direction = "stable"

        confidence = min(1.0, len(values) / 50)

        mk_p: float | None = None
        if len(values) >= MIN_SAMPLES_MANN_KENDALL:
            _, mk_p = _mann_kendall(values)
            if mk_p < 0.05:
                confidence = min(1.0, confidence + 0.2)

        return TrendAnalysis(
            metric_name=metric_name,
            direction=direction,
            change_percent=change_percent,
            baseline_value=baseline_avg,
            current_value=current_avg,
            confidence=confidence,
            period_days=7,
            mann_kendall_p=mk_p,
        )

    def detect_anomalies(self) -> list[AnomalyDetection]:
        """Detect anomalies using z-score on recent metrics."""
        task_metrics = self.collector.get_recent_task_metrics(hours=24)
        if len(task_metrics) < 5:
            return []

        anomalies: list[AnomalyDetection] = []
        costs = [m.cost_usd for m in task_metrics]
        mean_cost = sum(costs) / len(costs)
        std_cost = _std(costs)

        if std_cost == 0:
            return []

        for i, cost in enumerate(costs):
            z_score = (cost - mean_cost) / std_cost
            if abs(z_score) > 2.5:
                severity: Literal["low", "medium", "high", "critical"]
                if abs(z_score) > 4:
                    severity = "critical"
                elif abs(z_score) > 3:
                    severity = "high"
                elif abs(z_score) > 2.5:
                    severity = "medium"
                else:
                    severity = "low"

                anomalies.append(
                    AnomalyDetection(
                        metric_name="cost_per_task",
                        anomaly_type="spike" if z_score > 0 else "drop",
                        severity=severity,
                        z_score=z_score,
                        expected_value=mean_cost,
                        actual_value=cost,
                        timestamp=task_metrics[i].timestamp,
                        description=f"Cost anomaly detected: ${cost:.4f} vs expected ${mean_cost:.4f}",
                    )
                )

        return anomalies

    # -------------------------------------------------------------------
    # Full analysis pass
    # -------------------------------------------------------------------

    def run_full_analysis(self, hours: int = 168) -> dict[str, Any]:
        """Run complete analysis: trends + anomalies + BOCPD + posteriors."""
        task_metrics = self.collector.get_recent_task_metrics(hours=hours)
        self.ingest_task_metrics(task_metrics)

        result: dict[str, Any] = {
            "trends": self.analyze_trends(),
            "anomalies": self.detect_anomalies(),
            "changepoints": {},
            "ewma": {},
            "cusum": {},
            "posteriors": {},
            "composite": None,
            "n_records": len(task_metrics),
        }

        costs = [m.cost_usd for m in task_metrics]
        durations = [m.duration_seconds for m in task_metrics]

        if len(costs) >= MIN_SAMPLES_BOCPD:
            result["changepoints"]["cost"] = self.detect_changepoints(costs)
        if len(durations) >= MIN_SAMPLES_BOCPD:
            result["changepoints"]["duration"] = self.detect_changepoints(durations)

        for name in ("cost", "duration"):
            ewma = self.get_ewma_state(name)
            if ewma:
                result["ewma"][name] = ewma
            cusum = self.get_cusum_state(name)
            if cusum:
                result["cusum"][name] = cusum

        jp = self.get_beta_posterior("janitor_pass")
        if jp:
            result["posteriors"]["janitor_pass"] = {
                "mean": jp.mean,
                "ci_95": jp.ci_95,
                "n": jp.n_observations,
            }

        if task_metrics:
            n = len(task_metrics)
            pass_count = sum(1 for m in task_metrics if m.janitor_passed)
            avg_cost = sum(m.cost_usd for m in task_metrics) / n
            avg_dur = sum(m.duration_seconds for m in task_metrics) / n
            result["composite"] = self.compute_composite_score(
                {
                    "success_rate": pass_count / n,
                    "cost_efficiency": max(0, 1.0 - avg_cost),
                    "duration_efficiency": max(0, 1.0 - avg_dur / 600),
                    "code_quality": pass_count / n,
                    "retry_rate_inv": 1.0,
                }
            )

        if self._analysis_dir is not None:
            self._write_analysis_outputs(result)

        return result

    def _write_analysis_outputs(self, result: dict[str, Any]) -> None:
        """Write trends and anomalies to .sdd/analysis/."""
        if self._analysis_dir is None:
            return
        try:
            self._analysis_dir.mkdir(parents=True, exist_ok=True)

            trends_path = self._analysis_dir / "trends.json"
            trends_data = {
                "generated_at": time.time(),
                "period_days": 7,
                "trends": [asdict(t) for t in result.get("trends", [])],
            }
            trends_path.write_text(json.dumps(trends_data, indent=2), encoding="utf-8")

            anomalies_path = self._analysis_dir / "anomalies.json"
            anomalies_data = {
                "generated_at": time.time(),
                "anomalies": [asdict(a) for a in result.get("anomalies", [])],
            }
            anomalies_path.write_text(json.dumps(anomalies_data, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("Failed to write analysis outputs to %s", self._analysis_dir)

    # -------------------------------------------------------------------
    # Sample-size checks
    # -------------------------------------------------------------------

    def has_enough_data_for_alerting(self) -> bool:
        return len(self.collector.get_recent_task_metrics(hours=168)) >= MIN_SAMPLES_ALERTING

    def has_enough_data_for_ab(self) -> bool:
        return len(self.collector.get_recent_task_metrics(hours=168)) >= MIN_SAMPLES_AB

    def has_enough_data_for_trends(self) -> bool:
        return len(self.collector.get_recent_task_metrics(hours=168)) >= MIN_SAMPLES_TREND

    # -------------------------------------------------------------------
    # Failure pattern analysis
    # -------------------------------------------------------------------

    def analyze_failure_patterns(self, hours: int = 168) -> list[dict[str, Any]]:
        """Analyze failure patterns from recent task metrics.

        Groups failed tasks by role and computes per-role statistics including
        failure count, failure rate, involved models, and average cost of
        failures.  Only roles with >= 3 failures are included.

        Args:
            hours: Number of hours to look back (default 168 = 7 days).

        Returns:
            List of dicts, each containing:
            - role: the task role
            - failure_count: number of failed tasks for the role
            - total_count: total tasks for the role
            - failure_rate: failure_count / total_count
            - models_involved: list of distinct models that failed
            - avg_cost_of_failures: mean cost_usd across failed tasks
        """
        task_metrics = self.collector.get_recent_task_metrics(hours=hours)
        if not task_metrics:
            return []

        # Group all tasks and failed tasks by role
        role_all: dict[str, list[Any]] = {}
        role_failed: dict[str, list[Any]] = {}
        for m in task_metrics:
            role = m.role or "unknown"
            role_all.setdefault(role, []).append(m)
            if not m.janitor_passed:
                role_failed.setdefault(role, []).append(m)

        results: list[dict[str, Any]] = []
        for role, failed in role_failed.items():
            if len(failed) < 3:
                continue

            total = len(role_all.get(role, []))
            models = list({m.model for m in failed if m.model is not None})
            avg_cost = sum(m.cost_usd for m in failed) / len(failed)

            results.append(
                {
                    "role": role,
                    "failure_count": len(failed),
                    "total_count": total,
                    "failure_rate": len(failed) / total if total > 0 else 0.0,
                    "models_involved": models,
                    "avg_cost_of_failures": avg_cost,
                }
            )

        return results
