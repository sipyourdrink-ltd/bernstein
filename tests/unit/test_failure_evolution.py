"""Tests for failure-driven evolution: FailureRecord, FailureAnalyzer, OpportunityDetector, MetricsAggregator."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.evolution.aggregator import (
    FileMetricsCollector,
    MetricsAggregator,
    TaskMetrics,
)
from bernstein.evolution.detector import (
    FailureAnalyzer,
    FailureRecord,
    OpportunityDetector,
    UpgradeCategory,
)

# ---------------------------------------------------------------------------
# FailureRecord
# ---------------------------------------------------------------------------


def test_failure_record_to_dict() -> None:
    """All fields present in serialized dict, model can be None."""
    record = FailureRecord(
        timestamp=1000.0,
        task_id="t-1",
        role="backend",
        model=None,
        error_type="timeout",
    )
    d = record.to_dict()
    assert d == {
        "timestamp": 1000.0,
        "task_id": "t-1",
        "role": "backend",
        "model": None,
        "error_type": "timeout",
    }


def test_failure_record_to_dict_with_model() -> None:
    """Model value included when set."""
    record = FailureRecord(
        timestamp=2000.0,
        task_id="t-2",
        role="qa",
        model="sonnet",
        error_type="lint_fail",
    )
    d = record.to_dict()
    assert d["model"] == "sonnet"
    assert d["role"] == "qa"
    assert d["error_type"] == "lint_fail"


# ---------------------------------------------------------------------------
# FailureAnalyzer
# ---------------------------------------------------------------------------


def test_analyzer_init_creates_dirs(tmp_path: Path) -> None:
    """FailureAnalyzer(state_dir) creates the evolution directory."""
    FailureAnalyzer(state_dir=tmp_path)
    assert (tmp_path / "evolution").is_dir()


def test_record_failure_persists(tmp_path: Path) -> None:
    """Recorded failures are written to failures.jsonl."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    for i in range(3):
        analyzer.record_failure(f"t-{i}", "backend", "sonnet", "timeout")

    lines = (tmp_path / "evolution" / "failures.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        data = json.loads(line)
        assert "task_id" in data
        assert "error_type" in data


def test_record_failure_in_memory(tmp_path: Path) -> None:
    """In-memory _failures list grows with each record."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    assert len(analyzer._failures) == 0
    analyzer.record_failure("t-1", "backend", "sonnet", "timeout")
    assert len(analyzer._failures) == 1
    analyzer.record_failure("t-2", "qa", None, "lint_fail")
    assert len(analyzer._failures) == 2


def test_detect_patterns_below_threshold(tmp_path: Path) -> None:
    """Fewer than min_occurrences failures returns no patterns."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    analyzer.record_failure("t-1", "backend", "sonnet", "timeout")
    analyzer.record_failure("t-2", "backend", "sonnet", "timeout")

    patterns = analyzer.detect_patterns(min_occurrences=3)
    assert patterns == []


def test_detect_patterns_at_threshold(tmp_path: Path) -> None:
    """Exactly min_occurrences failures for a (role, error_type) yields one pattern."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    for i in range(3):
        analyzer.record_failure(f"t-{i}", "backend", "sonnet", "timeout")

    patterns = analyzer.detect_patterns(min_occurrences=3)
    assert len(patterns) == 1
    p = patterns[0]
    assert p.task_type == "backend"
    assert p.error_pattern == "timeout"
    assert p.occurrence_count == 3


def test_detect_patterns_multiple_groups(tmp_path: Path) -> None:
    """Distinct (role, error_type) groups produce separate patterns."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    for i in range(3):
        analyzer.record_failure(f"a-{i}", "backend", "sonnet", "timeout")
    for i in range(3):
        analyzer.record_failure(f"b-{i}", "qa", "haiku", "lint_fail")

    patterns = analyzer.detect_patterns(min_occurrences=3)
    assert len(patterns) == 2
    types = {(p.task_type, p.error_pattern) for p in patterns}
    assert ("backend", "timeout") in types
    assert ("qa", "lint_fail") in types


def test_detect_patterns_affected_models(tmp_path: Path) -> None:
    """Pattern aggregates all distinct models across matching failures."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    analyzer.record_failure("t-1", "backend", "sonnet", "timeout")
    analyzer.record_failure("t-2", "backend", "haiku", "timeout")
    analyzer.record_failure("t-3", "backend", "opus", "timeout")

    patterns = analyzer.detect_patterns(min_occurrences=3)
    assert len(patterns) == 1
    assert sorted(patterns[0].affected_models) == ["haiku", "opus", "sonnet"]


def test_detect_patterns_sample_task_ids(tmp_path: Path) -> None:
    """Only the first 5 task_ids are captured as samples."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    for i in range(7):
        analyzer.record_failure(f"t-{i}", "backend", "sonnet", "timeout")

    patterns = analyzer.detect_patterns(min_occurrences=3)
    assert len(patterns) == 1
    assert patterns[0].sample_task_ids == [f"t-{i}" for i in range(5)]
    assert patterns[0].occurrence_count == 7


def test_failure_rate_by_role(tmp_path: Path) -> None:
    """Failure rates across roles sum to 1.0."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    # 2 backend failures, 1 qa failure
    analyzer.record_failure("t-1", "backend", "sonnet", "timeout")
    analyzer.record_failure("t-2", "backend", "sonnet", "timeout")
    analyzer.record_failure("t-3", "qa", "haiku", "lint_fail")

    rates = analyzer.get_failure_rate_by_role(hours=24)
    assert pytest.approx(rates["backend"], abs=1e-9) == 2 / 3
    assert pytest.approx(rates["qa"], abs=1e-9) == 1 / 3
    assert pytest.approx(sum(rates.values()), abs=1e-9) == 1.0


def test_failure_rate_by_role_empty(tmp_path: Path) -> None:
    """No failures yields an empty dict."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    assert analyzer.get_failure_rate_by_role(hours=24) == {}


def test_failure_rate_by_model(tmp_path: Path) -> None:
    """Failure rates by model reflect per-model distribution."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    analyzer.record_failure("t-1", "backend", "sonnet", "timeout")
    analyzer.record_failure("t-2", "backend", "haiku", "timeout")
    analyzer.record_failure("t-3", "qa", "sonnet", "lint_fail")

    rates = analyzer.get_failure_rate_by_model(hours=24)
    assert pytest.approx(rates["sonnet"], abs=1e-9) == 2 / 3
    assert pytest.approx(rates["haiku"], abs=1e-9) == 1 / 3


def test_failure_rate_by_model_none_model(tmp_path: Path) -> None:
    """None model is reported as 'unknown'."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    analyzer.record_failure("t-1", "backend", None, "timeout")

    rates = analyzer.get_failure_rate_by_model(hours=24)
    assert "unknown" in rates
    assert rates["unknown"] == 1.0


def test_analyzer_loads_from_file(tmp_path: Path) -> None:
    """A new FailureAnalyzer with the same state_dir loads persisted records."""
    analyzer1 = FailureAnalyzer(state_dir=tmp_path)
    analyzer1.record_failure("t-1", "backend", "sonnet", "timeout")
    analyzer1.record_failure("t-2", "qa", None, "lint_fail")

    analyzer2 = FailureAnalyzer(state_dir=tmp_path)
    assert len(analyzer2._failures) == 2
    assert analyzer2._failures[0].task_id == "t-1"
    assert analyzer2._failures[1].task_id == "t-2"


def test_failure_rate_time_window(tmp_path: Path) -> None:
    """Only failures within the time window are counted."""
    analyzer = FailureAnalyzer(state_dir=tmp_path)

    # Record a failure and then manually backdate it
    analyzer.record_failure("t-old", "backend", "sonnet", "timeout")
    analyzer._failures[-1] = FailureRecord(
        timestamp=time.time() - 48 * 3600,  # 48 hours ago
        task_id="t-old",
        role="backend",
        model="sonnet",
        error_type="timeout",
    )

    analyzer.record_failure("t-new", "qa", "haiku", "lint_fail")

    rates = analyzer.get_failure_rate_by_role(hours=24)
    assert "backend" not in rates
    assert "qa" in rates
    assert rates["qa"] == 1.0


# ---------------------------------------------------------------------------
# OpportunityDetector — failure integration
# ---------------------------------------------------------------------------


def test_identify_failure_opportunities_no_analyzer(tmp_path: Path) -> None:
    """Without a FailureAnalyzer, identify_failure_opportunities returns []."""
    collector = FileMetricsCollector(tmp_path)
    detector = OpportunityDetector(collector, failure_analyzer=None)
    assert detector.identify_failure_opportunities() == []


def test_identify_failure_opportunities_single_model(tmp_path: Path) -> None:
    """Failures on a single model produce a MODEL_ROUTING opportunity."""
    collector = FileMetricsCollector(tmp_path)
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    for i in range(3):
        analyzer.record_failure(f"t-{i}", "backend", "sonnet", "timeout")

    detector = OpportunityDetector(collector, failure_analyzer=analyzer)
    opps = detector.identify_failure_opportunities()
    assert len(opps) == 1
    assert opps[0].category == UpgradeCategory.MODEL_ROUTING
    assert "sonnet" in opps[0].title


def test_identify_failure_opportunities_multi_model(tmp_path: Path) -> None:
    """Failures across 2+ models for a role produce a ROLE_TEMPLATES opportunity."""
    collector = FileMetricsCollector(tmp_path)
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    analyzer.record_failure("t-1", "backend", "sonnet", "timeout")
    analyzer.record_failure("t-2", "backend", "haiku", "timeout")
    analyzer.record_failure("t-3", "backend", "opus", "timeout")

    detector = OpportunityDetector(collector, failure_analyzer=analyzer)
    opps = detector.identify_failure_opportunities()
    assert len(opps) == 1
    assert opps[0].category == UpgradeCategory.ROLE_TEMPLATES
    assert "backend" in opps[0].title


def test_identify_opportunities_includes_failures(tmp_path: Path) -> None:
    """identify_opportunities() includes failure-driven opportunities."""
    collector = FileMetricsCollector(tmp_path)
    analyzer = FailureAnalyzer(state_dir=tmp_path)
    for i in range(3):
        analyzer.record_failure(f"t-{i}", "backend", "sonnet", "timeout")

    detector = OpportunityDetector(collector, failure_analyzer=analyzer)
    opps = detector.identify_opportunities()

    # There should be at least the failure opportunity
    failure_opps = [o for o in opps if "sonnet" in o.title or "backend" in o.title]
    assert len(failure_opps) >= 1


# ---------------------------------------------------------------------------
# MetricsAggregator — failure pattern analysis
# ---------------------------------------------------------------------------


def test_analyze_failure_patterns_empty(tmp_path: Path) -> None:
    """No metrics yields no failure patterns."""
    collector = FileMetricsCollector(tmp_path)
    agg = MetricsAggregator(collector)
    assert agg.analyze_failure_patterns() == []


def test_analyze_failure_patterns_below_threshold(tmp_path: Path) -> None:
    """Fewer than 3 failures for a role yields no results."""
    collector = FileMetricsCollector(tmp_path)
    for i in range(2):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=time.time(),
                task_id=f"t-{i}",
                role="backend",
                model="sonnet",
                cost_usd=0.10,
                janitor_passed=False,
            )
        )
    agg = MetricsAggregator(collector)
    assert agg.analyze_failure_patterns(hours=24) == []


def test_analyze_failure_patterns_at_threshold(tmp_path: Path) -> None:
    """3 failures for 'backend' returns one entry with correct fields."""
    collector = FileMetricsCollector(tmp_path)
    for i in range(3):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=time.time(),
                task_id=f"t-{i}",
                role="backend",
                model="sonnet",
                cost_usd=0.10,
                janitor_passed=False,
            )
        )
    agg = MetricsAggregator(collector)
    results = agg.analyze_failure_patterns(hours=24)
    assert len(results) == 1
    r = results[0]
    assert r["role"] == "backend"
    assert r["failure_count"] == 3
    assert r["total_count"] == 3
    assert pytest.approx(r["failure_rate"], abs=1e-9) == 1.0
    assert pytest.approx(r["avg_cost_of_failures"], abs=1e-9) == 0.10


def test_analyze_failure_patterns_multiple_roles(tmp_path: Path) -> None:
    """Only roles with >= 3 failures are returned."""
    collector = FileMetricsCollector(tmp_path)

    # 3 backend failures
    for i in range(3):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=time.time(),
                task_id=f"b-{i}",
                role="backend",
                model="sonnet",
                cost_usd=0.10,
                janitor_passed=False,
            )
        )

    # 2 qa failures — below threshold
    for i in range(2):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=time.time(),
                task_id=f"q-{i}",
                role="qa",
                model="haiku",
                cost_usd=0.05,
                janitor_passed=False,
            )
        )

    # 3 security failures
    for i in range(3):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=time.time(),
                task_id=f"s-{i}",
                role="security",
                model="opus",
                cost_usd=0.20,
                janitor_passed=False,
            )
        )

    agg = MetricsAggregator(collector)
    results = agg.analyze_failure_patterns(hours=24)
    roles = {r["role"] for r in results}
    assert "backend" in roles
    assert "security" in roles
    assert "qa" not in roles


def test_analyze_failure_patterns_models_involved(tmp_path: Path) -> None:
    """models_involved lists the distinct models that failed for the role."""
    collector = FileMetricsCollector(tmp_path)
    models = ["sonnet", "haiku", "sonnet"]
    for i, model in enumerate(models):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=time.time(),
                task_id=f"t-{i}",
                role="backend",
                model=model,
                cost_usd=0.10,
                janitor_passed=False,
            )
        )

    agg = MetricsAggregator(collector)
    results = agg.analyze_failure_patterns(hours=24)
    assert len(results) == 1
    assert sorted(results[0]["models_involved"]) == ["haiku", "sonnet"]
