"""Comprehensive tests for the cost anomaly detection module.

Covers per-task ceiling, burn rate, token ratio, retry spiral,
model mismatch, baseline persistence, audit trail, signal dedup,
disabled mode, and edge cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.cost_anomaly import (
    AnomalySignal,
    CostAnomalyDetector,
)
from bernstein.core.cost_tracker import BudgetStatus
from bernstein.core.models import CostAnomalyConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sdd_dir(tmp_path: Path) -> Path:
    """Create a minimal .sdd directory."""
    sdd_dir = tmp_path / ".sdd"
    (sdd_dir / "metrics").mkdir(parents=True, exist_ok=True)
    return sdd_dir


def _make_detector(tmp_path: Path, **config_overrides: object) -> CostAnomalyDetector:
    """Build a CostAnomalyDetector with optional config overrides."""
    config = CostAnomalyConfig(**config_overrides)  # type: ignore[arg-type]
    _sdd_dir(tmp_path)  # ensure directory exists
    return CostAnomalyDetector(config, tmp_path)


class _MockCostTracker:
    """Minimal mock for CostTracker.status()."""

    def __init__(self, spent: float, budget: float) -> None:
        self._spent = spent
        self._budget = budget

    def status(self) -> BudgetStatus:
        """Return a BudgetStatus snapshot."""
        return BudgetStatus(
            run_id="test",
            budget_usd=self._budget,
            spent_usd=self._spent,
            remaining_usd=max(self._budget - self._spent, 0),
            percentage_used=self._spent / self._budget if self._budget > 0 else 0,
            should_warn=False,
            should_stop=False,
        )


def _complete_tasks(
    detector: CostAnomalyDetector,
    complexity: str,
    cost_usd: float,
    count: int,
    *,
    tokens_in: int = 1000,
    tokens_out: int = 1500,
) -> list[AnomalySignal]:
    """Complete N identical tasks through the detector, return all signals."""
    all_signals: list[AnomalySignal] = []
    for i in range(count):
        sigs = detector.check_task_completion(
            task_id=f"task-{complexity}-{i}",
            complexity=complexity,
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        all_signals.extend(sigs)
    return all_signals


# ---------------------------------------------------------------------------
# 1. Per-Task Ceiling Tests
# ---------------------------------------------------------------------------


def test_per_task_ceiling_no_signal_during_warmup(tmp_path: Path) -> None:
    """Completing fewer than baseline_min_samples tasks skips ceiling checks."""
    detector = _make_detector(tmp_path, baseline_min_samples=5)

    # Complete 3 tasks at high cost (below the 5-sample threshold).
    signals = _complete_tasks(detector, "medium", cost_usd=5.00, count=3)

    ceiling_signals = [s for s in signals if s.rule == "per_task_ceiling"]
    assert ceiling_signals == []


def test_per_task_ceiling_warning_at_3x_median(tmp_path: Path) -> None:
    """A task costing 3.5x the median triggers a warning signal."""
    detector = _make_detector(tmp_path, baseline_min_samples=5)

    # Establish baseline: 6 medium tasks at $0.10 each.
    _complete_tasks(detector, "medium", cost_usd=0.10, count=6)

    # Now one at $0.35 (3.5x median).
    signals = detector.check_task_completion(
        task_id="expensive-task",
        complexity="medium",
        cost_usd=0.35,
        tokens_in=1000,
        tokens_out=1500,
    )

    ceiling_signals = [s for s in signals if s.rule == "per_task_ceiling"]
    assert len(ceiling_signals) == 1
    assert ceiling_signals[0].severity == "warning"
    assert ceiling_signals[0].action == "log"
    assert ceiling_signals[0].task_id == "expensive-task"


def test_per_task_ceiling_critical_at_6x_median(tmp_path: Path) -> None:
    """A task costing 6.5x the median triggers a critical kill signal."""
    detector = _make_detector(tmp_path, baseline_min_samples=5)

    _complete_tasks(detector, "medium", cost_usd=0.10, count=6)

    signals = detector.check_task_completion(
        task_id="very-expensive",
        complexity="medium",
        cost_usd=0.65,
        tokens_in=1000,
        tokens_out=1500,
    )

    ceiling_signals = [s for s in signals if s.rule == "per_task_ceiling"]
    assert len(ceiling_signals) == 1
    assert ceiling_signals[0].severity == "critical"
    assert ceiling_signals[0].action == "kill_agent"


def test_per_task_ceiling_normal_cost_no_signal(tmp_path: Path) -> None:
    """Tasks near the median produce no per-task ceiling signals."""
    detector = _make_detector(tmp_path, baseline_min_samples=5)

    _complete_tasks(detector, "medium", cost_usd=0.10, count=6)

    # Task at $0.12 is only 1.2x median -- well within threshold.
    signals = detector.check_task_completion(
        task_id="normal-task",
        complexity="medium",
        cost_usd=0.12,
        tokens_in=1000,
        tokens_out=1500,
    )

    ceiling_signals = [s for s in signals if s.rule == "per_task_ceiling"]
    assert ceiling_signals == []


def test_per_task_ceiling_different_tiers_independent(tmp_path: Path) -> None:
    """An expensive 'large' task does not trigger the 'small' tier ceiling."""
    detector = _make_detector(tmp_path, baseline_min_samples=5)

    # Build baselines for both tiers.
    _complete_tasks(detector, "small", cost_usd=0.05, count=6)
    _complete_tasks(detector, "large", cost_usd=1.00, count=6)

    # A large task at $2.50 is 2.5x the large median -- below 3x threshold.
    signals = detector.check_task_completion(
        task_id="big-task",
        complexity="large",
        cost_usd=2.50,
        tokens_in=2000,
        tokens_out=3000,
    )

    ceiling_signals = [s for s in signals if s.rule == "per_task_ceiling"]
    assert ceiling_signals == []


# ---------------------------------------------------------------------------
# 2. Burn Rate Tests
# ---------------------------------------------------------------------------


def test_burn_rate_no_signal_without_budget(tmp_path: Path) -> None:
    """Zero budget (unlimited) never triggers burn-rate signals."""
    detector = _make_detector(tmp_path)
    tracker = _MockCostTracker(spent=50.0, budget=0.0)

    signals = detector.check_tick(agents=[], cost_tracker=tracker)  # type: ignore[arg-type]

    burn_signals = [s for s in signals if s.rule == "burn_rate"]
    assert burn_signals == []


def test_burn_rate_warning_at_60pct(tmp_path: Path) -> None:
    """Spending 65% of budget triggers a warning signal."""
    detector = _make_detector(tmp_path, budget_warn_pct=60.0)
    tracker = _MockCostTracker(spent=6.5, budget=10.0)

    signals = detector.check_tick(agents=[], cost_tracker=tracker)  # type: ignore[arg-type]

    burn_signals = [s for s in signals if s.rule == "burn_rate"]
    assert len(burn_signals) == 1
    assert burn_signals[0].severity == "warning"
    assert burn_signals[0].action == "log"


def test_burn_rate_critical_stops_spawning_at_90pct(tmp_path: Path) -> None:
    """Spending 92% of budget triggers a critical stop_spawning signal."""
    detector = _make_detector(tmp_path, budget_stop_pct=90.0)
    tracker = _MockCostTracker(spent=9.2, budget=10.0)

    signals = detector.check_tick(agents=[], cost_tracker=tracker)  # type: ignore[arg-type]

    burn_signals = [s for s in signals if s.rule == "burn_rate"]
    assert len(burn_signals) == 1
    assert burn_signals[0].severity == "critical"
    assert burn_signals[0].action == "stop_spawning"


def test_burn_rate_below_threshold_no_signal(tmp_path: Path) -> None:
    """Spending 40% of budget is below all thresholds -- no signal."""
    detector = _make_detector(tmp_path, budget_warn_pct=60.0)
    tracker = _MockCostTracker(spent=4.0, budget=10.0)

    signals = detector.check_tick(agents=[], cost_tracker=tracker)  # type: ignore[arg-type]

    burn_signals = [s for s in signals if s.rule == "burn_rate"]
    assert burn_signals == []


# ---------------------------------------------------------------------------
# 3. Token Ratio Tests
# ---------------------------------------------------------------------------


def test_token_ratio_explosion_detected(tmp_path: Path) -> None:
    """Output/input ratio of 8.0 with sufficient tokens triggers a critical signal."""
    detector = _make_detector(tmp_path, token_ratio_max=5.0, token_ratio_min_tokens=5000)

    signals = detector.check_task_completion(
        task_id="chatty-task",
        complexity="medium",
        cost_usd=0.10,
        tokens_in=1000,
        tokens_out=8000,
    )

    ratio_signals = [s for s in signals if s.rule == "token_ratio"]
    assert len(ratio_signals) == 1
    assert ratio_signals[0].severity == "critical"
    assert ratio_signals[0].action == "kill_agent"
    assert ratio_signals[0].details["ratio"] == pytest.approx(8.0)


def test_token_ratio_below_threshold_no_signal(tmp_path: Path) -> None:
    """Output/input ratio of 3.0 is below the 5.0 threshold -- no signal."""
    detector = _make_detector(tmp_path, token_ratio_max=5.0, token_ratio_min_tokens=5000)

    signals = detector.check_task_completion(
        task_id="normal-task",
        complexity="medium",
        cost_usd=0.10,
        tokens_in=2000,
        tokens_out=6000,
    )

    ratio_signals = [s for s in signals if s.rule == "token_ratio"]
    assert ratio_signals == []


def test_token_ratio_skipped_below_min_tokens(tmp_path: Path) -> None:
    """High ratio with too few total tokens is ignored."""
    detector = _make_detector(tmp_path, token_ratio_max=5.0, token_ratio_min_tokens=5000)

    signals = detector.check_task_completion(
        task_id="tiny-task",
        complexity="small",
        cost_usd=0.01,
        tokens_in=100,
        tokens_out=800,
    )

    ratio_signals = [s for s in signals if s.rule == "token_ratio"]
    assert ratio_signals == []


# ---------------------------------------------------------------------------
# 4. Retry Spiral Tests
# ---------------------------------------------------------------------------


def test_retry_spiral_detected(tmp_path: Path) -> None:
    """Cumulative retry cost exceeding 2x the original triggers a spiral signal."""
    detector = _make_detector(tmp_path, retry_cost_multiplier=2.0)

    # Original task completes at $0.10.
    detector.check_task_completion(
        task_id="orig-1",
        complexity="medium",
        cost_usd=0.10,
        tokens_in=1000,
        tokens_out=1500,
    )

    # Retry 1: $0.08 (total retries = $0.08 < $0.20 threshold).
    signals_r1 = detector.check_task_completion(
        task_id="retry-1a",
        complexity="medium",
        cost_usd=0.08,
        tokens_in=1000,
        tokens_out=1500,
        is_retry=True,
        original_task_id="orig-1",
    )
    spiral_r1 = [s for s in signals_r1 if s.rule == "retry_spiral"]
    assert spiral_r1 == []

    # Retry 2: $0.08 (total retries = $0.16 < $0.20).
    signals_r2 = detector.check_task_completion(
        task_id="retry-1b",
        complexity="medium",
        cost_usd=0.08,
        tokens_in=1000,
        tokens_out=1500,
        is_retry=True,
        original_task_id="orig-1",
    )
    spiral_r2 = [s for s in signals_r2 if s.rule == "retry_spiral"]
    assert spiral_r2 == []

    # Retry 3: $0.08 (total retries = $0.24 > $0.20).
    signals_r3 = detector.check_task_completion(
        task_id="retry-1c",
        complexity="medium",
        cost_usd=0.08,
        tokens_in=1000,
        tokens_out=1500,
        is_retry=True,
        original_task_id="orig-1",
    )
    spiral_r3 = [s for s in signals_r3 if s.rule == "retry_spiral"]
    assert len(spiral_r3) == 1
    assert spiral_r3[0].severity == "critical"
    assert spiral_r3[0].action == "stop_spawning"
    assert spiral_r3[0].details["cumulative"] > spiral_r3[0].details["first_cost"] * 2


def test_retry_spiral_no_signal_for_normal_retry(tmp_path: Path) -> None:
    """A single retry below the cost threshold produces no spiral signal."""
    detector = _make_detector(tmp_path, retry_cost_multiplier=2.0)

    # Original at $0.10.
    detector.check_task_completion(
        task_id="orig-2",
        complexity="medium",
        cost_usd=0.10,
        tokens_in=1000,
        tokens_out=1500,
    )

    # Single retry at $0.10 (total retries = $0.10, threshold = $0.20).
    signals = detector.check_task_completion(
        task_id="retry-2a",
        complexity="medium",
        cost_usd=0.10,
        tokens_in=1000,
        tokens_out=1500,
        is_retry=True,
        original_task_id="orig-2",
    )

    spiral_signals = [s for s in signals if s.rule == "retry_spiral"]
    assert spiral_signals == []


# ---------------------------------------------------------------------------
# 5. Model Mismatch Tests
# ---------------------------------------------------------------------------


def test_model_mismatch_opus_on_small_task(tmp_path: Path) -> None:
    """Using claude-opus-4 on a small task triggers an info signal."""
    detector = _make_detector(tmp_path)

    signals = detector.check_spawn(
        task_id="small-task",
        complexity="small",
        model="claude-opus-4",
    )

    mismatch_signals = [s for s in signals if s.rule == "model_mismatch"]
    assert len(mismatch_signals) == 1
    assert mismatch_signals[0].severity == "info"
    assert mismatch_signals[0].action == "log"


def test_model_mismatch_sonnet_on_small_ok(tmp_path: Path) -> None:
    """Using claude-sonnet-4 on a small task is appropriate -- no signal."""
    detector = _make_detector(tmp_path)

    signals = detector.check_spawn(
        task_id="small-task",
        complexity="small",
        model="claude-sonnet-4",
    )

    mismatch_signals = [s for s in signals if s.rule == "model_mismatch"]
    assert mismatch_signals == []


def test_model_mismatch_opus_on_large_ok(tmp_path: Path) -> None:
    """Using claude-opus-4 on a large task is appropriate -- no signal."""
    detector = _make_detector(tmp_path)

    signals = detector.check_spawn(
        task_id="large-task",
        complexity="large",
        model="claude-opus-4",
    )

    mismatch_signals = [s for s in signals if s.rule == "model_mismatch"]
    assert mismatch_signals == []


# ---------------------------------------------------------------------------
# 6. Baseline Persistence Tests
# ---------------------------------------------------------------------------


def test_baseline_save_and_load_round_trip(tmp_path: Path) -> None:
    """Saving and loading a baseline preserves per-tier stats and token ratios."""
    detector1 = _make_detector(tmp_path)

    # Build a baseline with some completions.
    _complete_tasks(detector1, "medium", cost_usd=0.10, count=6)
    _complete_tasks(detector1, "large", cost_usd=0.50, count=4)
    detector1.save_baseline()

    # New detector, same directory.
    detector2 = _make_detector(tmp_path)
    detector2.load_baseline()

    # Verify per-tier stats survived round-trip.
    baseline = detector2._baseline
    assert "medium" in baseline.per_tier
    assert "large" in baseline.per_tier
    assert baseline.per_tier["medium"].sample_count == 6
    assert baseline.per_tier["large"].sample_count == 4
    assert baseline.per_tier["medium"].median_cost_usd == pytest.approx(0.10)
    assert baseline.per_tier["large"].median_cost_usd == pytest.approx(0.50)
    assert baseline.sample_count == 10


def test_baseline_rolling_window_capped(tmp_path: Path) -> None:
    """History is trimmed to the configured baseline_window size."""
    detector = _make_detector(tmp_path, baseline_window=5)

    # Complete 8 tasks.
    _complete_tasks(detector, "medium", cost_usd=0.10, count=8)

    # Internal history should be capped at 5.
    assert len(detector._recent_tasks) == 5


def test_load_baseline_missing_file_uses_empty(tmp_path: Path) -> None:
    """Loading from a directory with no baseline file starts with empty baseline."""
    detector = _make_detector(tmp_path)
    detector.load_baseline()

    baseline = detector._baseline
    assert baseline.per_tier == {}
    assert baseline.sample_count == 0


# ---------------------------------------------------------------------------
# 7. Audit Trail Tests
# ---------------------------------------------------------------------------


def test_record_signal_appends_to_anomalies_jsonl(tmp_path: Path) -> None:
    """Recording a signal writes one JSON line to the anomalies log."""
    detector = _make_detector(tmp_path)
    signal = AnomalySignal(
        rule="test_rule",
        severity="warning",
        action="log",
        agent_id="agent-1",
        task_id="task-1",
        message="Test anomaly detected",
        details={"key": "value"},
        timestamp=1000.0,
    )

    detector.record_signal(signal)

    anomalies_file = tmp_path / ".sdd" / "metrics" / "anomalies.jsonl"
    assert anomalies_file.exists()
    lines = anomalies_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["rule"] == "test_rule"
    assert data["severity"] == "warning"
    assert data["agent_id"] == "agent-1"
    assert data["task_id"] == "task-1"
    assert data["message"] == "Test anomaly detected"
    assert data["details"] == {"key": "value"}
    assert data["timestamp"] == pytest.approx(1000.0)


def test_multiple_signals_each_on_own_line(tmp_path: Path) -> None:
    """Recording 3 signals produces 3 separate JSON lines."""
    detector = _make_detector(tmp_path)

    for i in range(3):
        signal = AnomalySignal(
            rule=f"rule_{i}",
            severity="info",
            action="log",
            agent_id=None,
            task_id=f"task-{i}",
            message=f"Signal {i}",
            details={"index": i},
            timestamp=1000.0 + i,
        )
        detector.record_signal(signal)

    anomalies_file = tmp_path / ".sdd" / "metrics" / "anomalies.jsonl"
    lines = anomalies_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    for i, line in enumerate(lines):
        data = json.loads(line)
        assert data["rule"] == f"rule_{i}"
        assert data["details"]["index"] == i


# ---------------------------------------------------------------------------
# 8. Signal Dedup Tests
# ---------------------------------------------------------------------------


def test_log_signals_deduplicated_within_cooldown(tmp_path: Path) -> None:
    """Identical burn-rate warnings within the 60s cooldown are suppressed."""
    detector = _make_detector(tmp_path, budget_warn_pct=60.0)
    tracker = _MockCostTracker(spent=7.0, budget=10.0)

    first = detector.check_tick(agents=[], cost_tracker=tracker)  # type: ignore[arg-type]
    second = detector.check_tick(agents=[], cost_tracker=tracker)  # type: ignore[arg-type]

    burn_first = [s for s in first if s.rule == "burn_rate"]
    burn_second = [s for s in second if s.rule == "burn_rate"]
    assert len(burn_first) == 1
    assert burn_second == []


def test_kill_signals_never_deduplicated(tmp_path: Path) -> None:
    """Critical per-task ceiling signals (action=kill_agent) always fire."""
    detector = _make_detector(tmp_path, baseline_min_samples=5)

    # Build baseline.
    _complete_tasks(detector, "medium", cost_usd=0.10, count=6)

    # Two consecutive expensive tasks should both produce kill signals.
    signals_a = detector.check_task_completion(
        task_id="expensive-a",
        complexity="medium",
        cost_usd=0.65,
        tokens_in=1000,
        tokens_out=1500,
    )
    signals_b = detector.check_task_completion(
        task_id="expensive-b",
        complexity="medium",
        cost_usd=0.70,
        tokens_in=1000,
        tokens_out=1500,
    )

    ceiling_a = [s for s in signals_a if s.rule == "per_task_ceiling" and s.action == "kill_agent"]
    ceiling_b = [s for s in signals_b if s.rule == "per_task_ceiling" and s.action == "kill_agent"]
    assert len(ceiling_a) == 1
    assert len(ceiling_b) == 1


# ---------------------------------------------------------------------------
# 9. Disabled Tests
# ---------------------------------------------------------------------------


def test_disabled_detector_returns_no_signals(tmp_path: Path) -> None:
    """When enabled=False, all check methods return empty lists."""
    detector = _make_detector(tmp_path, enabled=False)
    tracker = _MockCostTracker(spent=9.5, budget=10.0)

    tick_signals = detector.check_tick(agents=[], cost_tracker=tracker)  # type: ignore[arg-type]
    completion_signals = detector.check_task_completion(
        task_id="task-1",
        complexity="medium",
        cost_usd=100.0,
        tokens_in=100,
        tokens_out=100_000,
    )
    spawn_signals = detector.check_spawn(
        task_id="task-2",
        complexity="small",
        model="claude-opus-4",
    )

    assert tick_signals == []
    assert completion_signals == []
    assert spawn_signals == []


# ---------------------------------------------------------------------------
# 10. Edge Cases
# ---------------------------------------------------------------------------


def test_zero_cost_task_no_crash(tmp_path: Path) -> None:
    """Completing a task with cost_usd=0.0 does not crash or signal."""
    detector = _make_detector(tmp_path, baseline_min_samples=5)

    # Build baseline so ceiling checks are active.
    _complete_tasks(detector, "medium", cost_usd=0.10, count=6)

    # Zero cost should not crash (0 / 0.10 = 0x median, well below threshold).
    signals = detector.check_task_completion(
        task_id="free-task",
        complexity="medium",
        cost_usd=0.0,
        tokens_in=500,
        tokens_out=500,
    )

    ceiling_signals = [s for s in signals if s.rule == "per_task_ceiling"]
    assert ceiling_signals == []


def test_zero_input_tokens_no_division_error(tmp_path: Path) -> None:
    """Zero input tokens does not cause a ZeroDivisionError."""
    detector = _make_detector(tmp_path, token_ratio_max=5.0, token_ratio_min_tokens=5000)

    # tokens_in=0 with sufficient total tokens to trigger the check.
    signals = detector.check_task_completion(
        task_id="no-input-task",
        complexity="medium",
        cost_usd=0.10,
        tokens_in=0,
        tokens_out=6000,
    )

    # Should fire (6000/1 = 6000.0 > 5.0) but the key point is no crash.
    ratio_signals = [s for s in signals if s.rule == "token_ratio"]
    assert len(ratio_signals) == 1
    assert ratio_signals[0].details["ratio"] == pytest.approx(6000.0)
