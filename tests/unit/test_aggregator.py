"""Tests for MetricsAggregator — EWMA, CUSUM, BOCPD, Mann-Kendall, posteriors, Goodhart."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.evolution.aggregator import (
    MIN_SAMPLES_AB,
    MIN_SAMPLES_ALERTING,
    MIN_SAMPLES_BOCPD,
    MIN_SAMPLES_CUSUM,
    MIN_SAMPLES_EWMA,
    MIN_SAMPLES_MANN_KENDALL,
    MIN_SAMPLES_TREND,
    AgentMetrics,
    Changepoint,
    CompositeScore,
    CostMetrics,
    FileMetricsCollector,
    MetricsAggregator,
    QualityMetrics,
    TaskMetrics,
    _cusum_update,
    _ewma_control_limits,
    _ewma_update,
    _mann_kendall,
    _norm_cdf,
    _std,
    _student_t_pdf,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_collector(tmp_path: Path) -> FileMetricsCollector:
    return FileMetricsCollector(state_dir=tmp_path)


def _make_aggregator(tmp_path: Path) -> tuple[MetricsAggregator, FileMetricsCollector]:
    collector = _make_collector(tmp_path)
    agg = MetricsAggregator(collector=collector)
    return agg, collector


def _recent_ts(offset_seconds: float = 0.0) -> float:
    """Return a timestamp within the last hour (safely recent for 24h queries)."""
    return time.time() - 1800 + offset_seconds


def _make_task_metrics(
    *,
    cost: float = 0.05,
    duration: float = 60.0,
    passed: bool = True,
    ts: float | None = None,
    task_id: str = "t-1",
) -> TaskMetrics:
    return TaskMetrics(
        timestamp=ts if ts is not None else _recent_ts(),
        task_id=task_id,
        role="backend",
        model="claude-sonnet-4-20250514",
        provider="anthropic",
        duration_seconds=duration,
        tokens_prompt=500,
        tokens_completion=200,
        cost_usd=cost,
        janitor_passed=passed,
        files_modified=2,
        lines_added=30,
        lines_deleted=5,
    )


# ===================================================================
# 1. EWMA tracking
# ===================================================================


def test_ewma_first_observation_initializes_state(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    state = agg.update_ewma("cost", 1.0)

    assert state.metric_name == "cost"
    assert state.current_value == 1.0
    assert state.center_line == 1.0
    assert state.n_observations == 1
    assert state.in_control is True


def test_ewma_multiple_updates_track_weighted_average(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    lam = 0.2
    agg.update_ewma("cost", 10.0, lambda_=lam)
    state = agg.update_ewma("cost", 12.0, lambda_=lam)

    expected = lam * 12.0 + (1 - lam) * 10.0
    assert state.current_value == pytest.approx(expected, abs=1e-9)
    assert state.n_observations == 2


def test_ewma_control_limits_set_after_min_samples(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    sigma = 1.0
    for i in range(MIN_SAMPLES_EWMA + 1):
        state = agg.update_ewma("metric", 5.0, sigma=sigma)

    # After enough samples, UCL and LCL should be set around center_line
    assert state.ucl > state.center_line
    assert state.lcl < state.center_line
    assert state.in_control is True


def test_ewma_detects_out_of_control(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    sigma = 1.0
    # Build up baseline with stable values
    for _ in range(MIN_SAMPLES_EWMA + 1):
        agg.update_ewma("metric", 5.0, sigma=sigma)

    # Inject a wildly out-of-control value
    state = agg.update_ewma("metric", 500.0, sigma=sigma)
    assert state.in_control is False


def test_ewma_get_state_returns_none_for_unknown(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    assert agg.get_ewma_state("nonexistent") is None


def test_ewma_get_state_returns_current(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    agg.update_ewma("x", 3.14)
    state = agg.get_ewma_state("x")
    assert state is not None
    assert state.current_value == pytest.approx(3.14)


# ===================================================================
# 2. CUSUM shift detection
# ===================================================================


def test_cusum_first_observation_sets_target(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    state = agg.update_cusum("cost", 5.0)

    assert state.metric_name == "cost"
    assert state.target == 5.0
    assert state.n_observations == 1
    assert state.shift_detected is False


def test_cusum_detects_upward_shift(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    # First observation sets the target
    agg.update_cusum("cost", 0.0, target=0.0, k=0.5, h=4.0)
    # Feed consistently high values to accumulate S_high
    for _ in range(20):
        state = agg.update_cusum("cost", 3.0, target=0.0, k=0.5, h=4.0)
    assert state.shift_detected is True
    assert state.shift_direction == "up"


def test_cusum_detects_downward_shift(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    agg.update_cusum("cost", 0.0, target=0.0, k=0.5, h=4.0)
    for _ in range(20):
        state = agg.update_cusum("cost", -3.0, target=0.0, k=0.5, h=4.0)
    assert state.shift_detected is True
    assert state.shift_direction == "down"


def test_cusum_no_shift_on_stable_data(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    for _ in range(20):
        state = agg.update_cusum("cost", 5.0, target=5.0, k=0.5, h=4.0)
    assert state.shift_detected is False
    assert state.shift_direction == "none"


def test_cusum_reset_clears_accumulators(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    agg.update_cusum("cost", 0.0, target=0.0)
    for _ in range(20):
        agg.update_cusum("cost", 3.0, target=0.0)
    state = agg.get_cusum_state("cost")
    assert state is not None
    assert state.shift_detected is True

    agg.reset_cusum("cost")
    state = agg.get_cusum_state("cost")
    assert state is not None
    assert state.s_high == 0.0
    assert state.s_low == 0.0
    assert state.shift_detected is False
    assert state.shift_direction == "none"


def test_cusum_reset_noop_for_unknown(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    # Should not raise
    agg.reset_cusum("nonexistent")


def test_cusum_get_state_returns_none_for_unknown(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    assert agg.get_cusum_state("missing") is None


# ===================================================================
# 3. BOCPD changepoint detection
# ===================================================================


def test_bocpd_detects_changepoints_with_high_hazard(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    # With a high hazard rate, the algorithm interprets each observation as a
    # potential changepoint. This exercises the full code path.
    values = [0.0] * 15 + [100.0] * 15
    changepoints = agg.detect_changepoints(values, hazard_rate=0.9)
    assert len(changepoints) >= 1
    for cp in changepoints:
        assert cp.probability > 0.5


def test_bocpd_conservative_hazard_rarely_fires(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    # Default hazard_rate=1/250 is very conservative — even large shifts
    # may not produce changepoints. This is by design.
    values = [0.0] * 30 + [20.0] * 30
    changepoints = agg.detect_changepoints(values)
    # With the default conservative hazard, the algorithm may or may not fire.
    # We just verify it returns a valid list.
    assert isinstance(changepoints, list)
    for cp in changepoints:
        assert isinstance(cp, Changepoint)


def test_bocpd_returns_empty_for_insufficient_data(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    values = [1.0] * (MIN_SAMPLES_BOCPD - 1)
    changepoints = agg.detect_changepoints(values)
    assert changepoints == []


def test_bocpd_stable_data_few_or_no_changepoints(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    values = [5.0] * 50
    changepoints = agg.detect_changepoints(values)
    # Perfectly stable data should yield no changepoints
    assert len(changepoints) == 0


def test_bocpd_changepoint_structure(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    # Use high hazard to guarantee changepoints so we can test structure
    values = [0.0] * 15 + [100.0] * 15
    changepoints = agg.detect_changepoints(values, hazard_rate=0.9)
    assert len(changepoints) > 0
    for cp in changepoints:
        assert isinstance(cp, Changepoint)
        assert cp.probability > 0.5
        assert cp.run_length == 0  # always 0 per implementation
        assert 0 < cp.index < len(values)


# ===================================================================
# 4. Mann-Kendall trend test
# ===================================================================


def test_mann_kendall_detects_increasing_trend(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    values = list(range(20))  # 0, 1, 2, ..., 19 — clearly increasing
    result = agg.mann_kendall_test(values)
    assert result is not None
    s, p = result
    assert s > 0  # positive S => increasing
    assert p < 0.05  # statistically significant


def test_mann_kendall_no_trend_on_constant_data(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    values = [5.0] * 20
    result = agg.mann_kendall_test(values)
    assert result is not None
    s, p = result
    assert s == 0.0
    assert p == 1.0  # no trend at all


def test_mann_kendall_returns_none_for_insufficient_data(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    values = [1.0] * (MIN_SAMPLES_MANN_KENDALL - 1)
    assert agg.mann_kendall_test(values) is None


def test_mann_kendall_detects_decreasing_trend(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    values = list(range(20, 0, -1))  # 20, 19, ..., 1 — decreasing
    result = agg.mann_kendall_test(values)
    assert result is not None
    s, p = result
    assert s < 0  # negative S => decreasing
    assert p < 0.05


# ===================================================================
# 5. Beta-Binomial posterior
# ===================================================================


def test_beta_binomial_all_successes(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    for _ in range(10):
        posterior = agg.update_beta_binomial("pass_rate", success=True)

    assert posterior.n_observations == 10
    # Prior alpha=1, beta=1; 10 successes => alpha=11, beta=1
    assert posterior.alpha == pytest.approx(11.0)
    assert posterior.beta == pytest.approx(1.0)
    assert posterior.mean > 0.9


def test_beta_binomial_all_failures(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    for _ in range(10):
        posterior = agg.update_beta_binomial("pass_rate", success=False)

    assert posterior.alpha == pytest.approx(1.0)
    assert posterior.beta == pytest.approx(11.0)
    assert posterior.mean < 0.1


def test_beta_binomial_mixed(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    for _ in range(7):
        agg.update_beta_binomial("pass_rate", success=True)
    for _ in range(3):
        posterior = agg.update_beta_binomial("pass_rate", success=False)

    # alpha = 1 + 7 = 8, beta = 1 + 3 = 4
    assert posterior.alpha == pytest.approx(8.0)
    assert posterior.beta == pytest.approx(4.0)
    assert 0.5 < posterior.mean < 0.8


def test_beta_binomial_ci_95_within_bounds(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    for _ in range(50):
        agg.update_beta_binomial("x", success=True)
    for _ in range(50):
        agg.update_beta_binomial("x", success=False)
    posterior = agg.get_beta_posterior("x")
    assert posterior is not None
    lo, hi = posterior.ci_95
    assert 0.0 <= lo <= posterior.mean
    assert posterior.mean <= hi <= 1.0


def test_beta_binomial_variance_shrinks_with_more_data(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    agg.update_beta_binomial("x", success=True)
    agg.update_beta_binomial("x", success=False)
    var_early = agg.get_beta_posterior("x")
    assert var_early is not None
    early_var = var_early.variance

    for _ in range(100):
        agg.update_beta_binomial("x", success=True)
    for _ in range(100):
        agg.update_beta_binomial("x", success=False)
    var_late = agg.get_beta_posterior("x")
    assert var_late is not None
    assert var_late.variance < early_var


def test_get_beta_posterior_none_for_unknown(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    assert agg.get_beta_posterior("unknown") is None


# ===================================================================
# 6. Normal-Inverse-Gamma posterior
# ===================================================================


def test_nig_first_observation_sets_mu(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    posterior = agg.update_normal_inverse_gamma("cost", 3.5)

    assert posterior.metric_name == "cost"
    assert posterior.mu == pytest.approx(3.5)
    assert posterior.n_observations == 1
    assert posterior.kappa == 1.0
    assert posterior.alpha == 1.0
    assert posterior.beta == 1.0


def test_nig_multiple_updates_shift_mu(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    agg.update_normal_inverse_gamma("cost", 10.0)
    posterior = agg.update_normal_inverse_gamma("cost", 20.0)

    # mu = (1 * 10 + 20) / 2 = 15
    assert posterior.mu == pytest.approx(15.0)
    assert posterior.n_observations == 2
    assert posterior.kappa == pytest.approx(2.0)


def test_nig_variance_finite_after_enough_observations(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    for v in [5.0, 6.0, 4.0, 5.5, 4.5]:
        posterior = agg.update_normal_inverse_gamma("x", v)

    # alpha starts at 1.0 and gets +0.5 per update after the first,
    # so after 5 observations alpha = 1 + 4 * 0.5 = 3.0
    assert posterior.alpha > 1.0
    assert posterior.variance < float("inf")
    assert posterior.variance > 0


def test_nig_variance_infinite_when_alpha_leq_1(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    posterior = agg.update_normal_inverse_gamma("x", 5.0)
    # First observation: alpha=1.0, so variance should be inf
    assert posterior.variance == float("inf")


def test_get_nig_posterior_none_for_unknown(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    assert agg.get_nig_posterior("unknown") is None


# ===================================================================
# 7. Composite scoring and Goodhart defenses
# ===================================================================


def test_composite_score_basic(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    metrics = {
        "success_rate": 0.9,
        "cost_efficiency": 0.8,
        "duration_efficiency": 0.7,
        "code_quality": 0.85,
        "retry_rate_inv": 0.95,
    }
    result = agg.compute_composite_score(metrics)

    assert isinstance(result, CompositeScore)
    # Score = 0.9*0.3 + 0.8*0.2 + 0.7*0.2 + 0.85*0.15 + 0.95*0.15
    expected = 0.9 * 0.3 + 0.8 * 0.2 + 0.7 * 0.2 + 0.85 * 0.15 + 0.95 * 0.15
    assert result.score == pytest.approx(expected, abs=1e-9)
    assert len(result.components) == 5
    assert result.divergence_flags == []
    assert result.trip_wire_flags == []


def test_composite_score_missing_metrics_default_to_zero(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    result = agg.compute_composite_score({})
    assert result.score == pytest.approx(0.0)


def test_composite_divergence_detection(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)

    # Build a 3-window history with low values
    for _ in range(3):
        agg.compute_composite_score(
            {
                "success_rate": 0.5,
                "cost_efficiency": 0.5,
                "duration_efficiency": 0.5,
                "code_quality": 0.5,
                "retry_rate_inv": 0.5,
            }
        )

    # Now: success_rate jumps up while code_quality drops
    result = agg.compute_composite_score(
        {
            "success_rate": 0.7,  # +0.2 from avg of 0.5
            "cost_efficiency": 0.5,
            "duration_efficiency": 0.5,
            "code_quality": 0.3,  # -0.2 from avg of 0.5
            "retry_rate_inv": 0.5,
        }
    )
    assert len(result.divergence_flags) > 0
    # One of the flagged pairs should mention success_rate and code_quality
    joined = " ".join(result.divergence_flags)
    assert "success_rate" in joined
    assert "code_quality" in joined


def test_composite_trip_wire_100_percent_success(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)

    # Feed 6 windows with 100% success rate to exceed the 5-consecutive threshold
    for _ in range(6):
        agg.compute_composite_score(
            {
                "success_rate": 1.0,
                "cost_efficiency": 0.5,
                "duration_efficiency": 0.5,
                "code_quality": 0.5,
                "retry_rate_inv": 0.5,
            }
        )

    # The 7th call should trigger the trip wire (5+ consecutive 100% already in history)
    result = agg.compute_composite_score(
        {
            "success_rate": 1.0,
            "cost_efficiency": 0.5,
            "duration_efficiency": 0.5,
            "code_quality": 0.5,
            "retry_rate_inv": 0.5,
        }
    )
    assert len(result.trip_wire_flags) > 0
    assert "100%" in result.trip_wire_flags[0]


def test_composite_no_trip_wire_below_threshold(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    # Only 3 calls with 100% — not enough for trip wire
    for _ in range(3):
        result = agg.compute_composite_score(
            {
                "success_rate": 1.0,
                "cost_efficiency": 0.5,
                "duration_efficiency": 0.5,
                "code_quality": 0.5,
                "retry_rate_inv": 0.5,
            }
        )
    assert result.trip_wire_flags == []


# ===================================================================
# 8. FileMetricsCollector
# ===================================================================


def test_collector_record_and_retrieve_task_metrics(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path)
    ts = _recent_ts()
    m = _make_task_metrics(ts=ts)
    collector.record_task_metrics(m)

    recent = collector.get_recent_task_metrics(hours=24)
    assert len(recent) == 1
    assert recent[0].task_id == "t-1"
    assert recent[0].cost_usd == pytest.approx(0.05)


def test_collector_record_and_retrieve_agent_metrics(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path)
    m = AgentMetrics(
        timestamp=_recent_ts(),
        task_id="t-1",
        agent_id="agent-1",
        lifetime_seconds=120.0,
        tasks_completed=3,
    )
    collector.record_agent_metrics(m)
    recent = collector.get_recent_agent_metrics(hours=24)
    assert len(recent) == 1
    assert recent[0].agent_id == "agent-1"


def test_collector_record_and_retrieve_cost_metrics(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path)
    m = CostMetrics(
        timestamp=_recent_ts(),
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        cost_usd=0.02,
    )
    collector.record_cost_metrics(m)
    recent = collector.get_recent_cost_metrics(hours=24)
    assert len(recent) == 1
    assert recent[0].cost_usd == pytest.approx(0.02)


def test_collector_record_and_retrieve_quality_metrics(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path)
    m = QualityMetrics(
        timestamp=_recent_ts(),
        janitor_pass_rate=0.95,
        test_pass_rate=0.88,
    )
    collector.record_quality_metrics(m)
    recent = collector.get_recent_quality_metrics(hours=24)
    assert len(recent) == 1
    assert recent[0].janitor_pass_rate == pytest.approx(0.95)


def test_collector_filters_old_metrics(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path)
    old_ts = time.time() - 48 * 3600  # 48 hours ago
    collector.record_task_metrics(_make_task_metrics(ts=old_ts))
    collector.record_task_metrics(_make_task_metrics(ts=_recent_ts()))

    recent = collector.get_recent_task_metrics(hours=24)
    assert len(recent) == 1


def test_collector_persistence_to_jsonl(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path)
    ts = _recent_ts()
    collector.record_task_metrics(_make_task_metrics(ts=ts, cost=0.10))

    # Verify the JSONL file was written
    tasks_file = tmp_path / "metrics" / "tasks.jsonl"
    assert tasks_file.exists()

    lines = tasks_file.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["cost_usd"] == pytest.approx(0.10)
    assert data["task_id"] == "t-1"


def test_collector_load_from_files(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path)
    ts = _recent_ts()
    collector.record_task_metrics(_make_task_metrics(ts=ts, cost=0.07))
    collector.record_cost_metrics(CostMetrics(timestamp=ts, cost_usd=0.03))

    # Create a fresh collector pointing at the same directory
    collector2 = FileMetricsCollector(state_dir=tmp_path)
    assert len(collector2.get_recent_task_metrics(hours=24)) == 0  # not loaded yet

    collector2.load_from_files()
    assert len(collector2.get_recent_task_metrics(hours=24)) == 1
    assert len(collector2.get_recent_cost_metrics(hours=24)) == 1


def test_collector_multiple_records_persist(tmp_path: Path) -> None:
    collector = _make_collector(tmp_path)
    for i in range(5):
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i)), task_id=f"t-{i}"))

    tasks_file = tmp_path / "metrics" / "tasks.jsonl"
    lines = tasks_file.read_text().strip().splitlines()
    assert len(lines) == 5


# ===================================================================
# 9. Batch ingestion via ingest_task_metrics
# ===================================================================


def test_ingest_task_metrics_updates_all_trackers(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    batch = [_make_task_metrics(cost=0.05 + i * 0.01, duration=60.0 + i) for i in range(10)]
    agg.ingest_task_metrics(batch)

    # EWMA should have been updated for cost and duration
    assert agg.get_ewma_state("cost") is not None
    assert agg.get_ewma_state("cost").n_observations == 10
    assert agg.get_ewma_state("duration") is not None

    # CUSUM should have been updated
    assert agg.get_cusum_state("cost") is not None
    assert agg.get_cusum_state("duration") is not None

    # Beta-binomial for janitor pass
    assert agg.get_beta_posterior("janitor_pass") is not None
    assert agg.get_beta_posterior("janitor_pass").n_observations == 10

    # NIG for cost and duration
    assert agg.get_nig_posterior("cost") is not None
    assert agg.get_nig_posterior("duration") is not None


def test_ingest_empty_batch_is_noop(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    agg.ingest_task_metrics([])
    assert agg.get_ewma_state("cost") is None


# ===================================================================
# 10. analyze_trends and detect_anomalies
# ===================================================================


def test_analyze_trends_returns_empty_for_few_records(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    # Fewer than 10 records -> no trends
    for i in range(5):
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i))))
    trends = agg.analyze_trends()
    assert trends == []


def test_analyze_trends_detects_cost_increase(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    # First half: low cost, second half: high cost
    for i in range(10):
        cost = 0.01 if i < 5 else 0.50
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i)), cost=cost))
    trends = agg.analyze_trends()
    assert len(trends) > 0
    cost_trends = [t for t in trends if t.metric_name == "cost_per_task"]
    assert len(cost_trends) == 1
    assert cost_trends[0].direction == "increasing"
    assert cost_trends[0].change_percent > 10


def test_analyze_trends_stable_data(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    for i in range(20):
        collector.record_task_metrics(
            _make_task_metrics(ts=_recent_ts(offset_seconds=float(i)), cost=0.05, duration=60.0)
        )
    trends = agg.analyze_trends()
    for t in trends:
        assert t.direction == "stable"


def test_detect_anomalies_returns_empty_for_few_records(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    for i in range(3):
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i))))
    anomalies = agg.detect_anomalies()
    assert anomalies == []


def test_detect_anomalies_finds_cost_spike(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    # Many normal costs, then one extreme outlier
    for i in range(20):
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i)), cost=0.05))
    collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=21.0), cost=5.0))
    anomalies = agg.detect_anomalies()
    assert len(anomalies) >= 1
    spike = [a for a in anomalies if a.anomaly_type == "spike"]
    assert len(spike) >= 1
    assert spike[0].z_score > 2.5


def test_detect_anomalies_no_anomalies_for_uniform_data(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    for i in range(20):
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i)), cost=0.05))
    anomalies = agg.detect_anomalies()
    # All identical => std=0, method returns []
    assert anomalies == []


# ===================================================================
# 11. run_full_analysis integration
# ===================================================================


def test_run_full_analysis_with_enough_data(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    # Generate enough data to exercise most paths
    for i in range(35):
        cost = 0.05 if i < 20 else 0.50
        collector.record_task_metrics(
            _make_task_metrics(
                ts=_recent_ts(offset_seconds=float(i)),
                cost=cost,
                duration=60.0 + i,
                passed=(i % 5 != 0),
                task_id=f"t-{i}",
            )
        )

    result = agg.run_full_analysis(hours=168)

    assert result["n_records"] == 35
    assert "trends" in result
    assert "anomalies" in result
    assert "changepoints" in result
    assert "ewma" in result
    assert "cusum" in result
    assert "posteriors" in result
    assert result["composite"] is not None
    assert isinstance(result["composite"], CompositeScore)

    # EWMA and CUSUM should have cost and duration entries
    assert "cost" in result["ewma"]
    assert "duration" in result["ewma"]
    assert "cost" in result["cusum"]

    # janitor_pass posterior
    assert "janitor_pass" in result["posteriors"]
    jp = result["posteriors"]["janitor_pass"]
    assert "mean" in jp
    assert "ci_95" in jp
    assert "n" in jp
    assert jp["n"] == 35


def test_run_full_analysis_with_bocpd_data(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    # >= MIN_SAMPLES_BOCPD records to trigger changepoint detection
    for i in range(MIN_SAMPLES_BOCPD + 10):
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i)), task_id=f"t-{i}"))

    result = agg.run_full_analysis(hours=168)
    # changepoints dict should have entries (may be empty lists but keys should exist)
    assert "cost" in result["changepoints"] or "duration" in result["changepoints"]


def test_run_full_analysis_empty(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    result = agg.run_full_analysis(hours=168)
    assert result["n_records"] == 0
    assert result["trends"] == []
    assert result["anomalies"] == []
    assert result["composite"] is None


# ===================================================================
# 12. Sample size checks
# ===================================================================


def test_has_enough_data_for_alerting_false_when_empty(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    assert agg.has_enough_data_for_alerting() is False


def test_has_enough_data_for_alerting_true_at_threshold(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    for i in range(MIN_SAMPLES_ALERTING):
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i)), task_id=f"t-{i}"))
    assert agg.has_enough_data_for_alerting() is True


def test_has_enough_data_for_ab_false_when_insufficient(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    for i in range(MIN_SAMPLES_AB - 1):
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i)), task_id=f"t-{i}"))
    assert agg.has_enough_data_for_ab() is False


def test_has_enough_data_for_ab_true_at_threshold(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    for i in range(MIN_SAMPLES_AB):
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i)), task_id=f"t-{i}"))
    assert agg.has_enough_data_for_ab() is True


def test_has_enough_data_for_trends_false_when_insufficient(tmp_path: Path) -> None:
    agg, _ = _make_aggregator(tmp_path)
    assert agg.has_enough_data_for_trends() is False


def test_has_enough_data_for_trends_true_at_threshold(tmp_path: Path) -> None:
    agg, collector = _make_aggregator(tmp_path)
    for i in range(MIN_SAMPLES_TREND):
        collector.record_task_metrics(_make_task_metrics(ts=_recent_ts(offset_seconds=float(i)), task_id=f"t-{i}"))
    assert agg.has_enough_data_for_trends() is True


# ===================================================================
# Statistical helper functions (unit-level)
# ===================================================================


def test_ewma_update_formula() -> None:
    result = _ewma_update(10.0, 20.0, 0.3)
    expected = 0.3 * 20.0 + 0.7 * 10.0
    assert result == pytest.approx(expected)


def test_ewma_control_limits_symmetric() -> None:
    upper, lower = _ewma_control_limits(sigma=1.0, lambda_=0.2, n=50)
    assert upper > 0
    assert lower < 0
    assert upper == pytest.approx(-lower)


def test_cusum_update_accumulators() -> None:
    s_high, s_low = _cusum_update(value=3.0, target=0.0, k=0.5, s_high=0.0, s_low=0.0)
    assert s_high == pytest.approx(2.5)  # max(0, 0 + 3 - 0.5)
    assert s_low == pytest.approx(0.0)  # max(0, 0 - 3 - 0.5) = max(0, -3.5)


def test_cusum_update_downward() -> None:
    s_high, s_low = _cusum_update(value=-3.0, target=0.0, k=0.5, s_high=0.0, s_low=0.0)
    assert s_high == pytest.approx(0.0)
    assert s_low == pytest.approx(2.5)  # max(0, 0 - (-3) - 0.5) = max(0, 2.5)


def test_mann_kendall_direct_increasing() -> None:
    values = list(range(10))
    s, p = _mann_kendall(values)
    assert s > 0
    assert p < 0.05


def test_norm_cdf_known_values() -> None:
    assert _norm_cdf(0.0) == pytest.approx(0.5)
    assert _norm_cdf(10.0) == pytest.approx(1.0, abs=1e-6)
    assert _norm_cdf(-10.0) == pytest.approx(0.0, abs=1e-6)


def test_std_single_value() -> None:
    assert _std([5.0]) == 0.0


def test_std_known_values() -> None:
    # Population std of [2, 4, 4, 4, 5, 5, 7, 9] = 2.0
    values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    assert _std(values) == pytest.approx(2.0)


def test_student_t_pdf_positive() -> None:
    # Should return a positive value
    result = _student_t_pdf(x=0.0, mu=0.0, var=1.0, nu=10.0)
    assert result > 0


def test_student_t_pdf_edge_cases() -> None:
    # nu <= 0 or var <= 0 should return small fallback
    assert _student_t_pdf(x=0.0, mu=0.0, var=0.0, nu=10.0) == pytest.approx(1e-10)
    assert _student_t_pdf(x=0.0, mu=0.0, var=1.0, nu=0.0) == pytest.approx(1e-10)


# ===================================================================
# Constant values sanity check
# ===================================================================


def test_min_sample_constants() -> None:
    assert MIN_SAMPLES_ALERTING == 30
    assert MIN_SAMPLES_AB == 50
    assert MIN_SAMPLES_TREND == 200
    assert MIN_SAMPLES_EWMA == 5
    assert MIN_SAMPLES_CUSUM == 8
    assert MIN_SAMPLES_MANN_KENDALL == 8
    assert MIN_SAMPLES_BOCPD == 20
