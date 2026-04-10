"""Tests for the load-testing harness — percentiles, report building, formatting, thresholds."""

from __future__ import annotations

import pytest

from bernstein.testing.load_test import (
    LoadTestConfig,
    LoadTestReport,
    RequestResult,
    build_load_test_report,
    check_thresholds,
    compute_percentiles,
    format_load_test_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    *,
    endpoint: str = "/status",
    status_code: int = 200,
    duration_ms: float = 5.0,
    error: str | None = None,
    timestamp: float = 1000.0,
) -> RequestResult:
    """Build a ``RequestResult`` with sensible defaults."""
    return RequestResult(
        endpoint=endpoint,
        status_code=status_code,
        duration_ms=duration_ms,
        error=error,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# TestComputePercentiles
# ---------------------------------------------------------------------------


class TestComputePercentiles:
    def test_empty_returns_zeros(self) -> None:
        pct = compute_percentiles([])
        assert pct == pytest.approx({"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0})

    def test_single_value(self) -> None:
        pct = compute_percentiles([42.0])
        assert pct["p50"] == pytest.approx(42.0)
        assert pct["p99"] == pytest.approx(42.0)
        assert pct["max"] == pytest.approx(42.0)

    def test_two_values(self) -> None:
        pct = compute_percentiles([10.0, 20.0])
        assert pct["p50"] == pytest.approx(10.0)
        assert pct["max"] == pytest.approx(20.0)

    def test_sorted_order_irrelevant(self) -> None:
        forward = compute_percentiles([1.0, 2.0, 3.0, 4.0, 5.0])
        reverse = compute_percentiles([5.0, 4.0, 3.0, 2.0, 1.0])
        assert forward == reverse

    def test_100_values_known_percentiles(self) -> None:
        durations = [float(i) for i in range(1, 101)]
        pct = compute_percentiles(durations)
        assert pct["p50"] == pytest.approx(50.0)
        assert pct["p95"] == pytest.approx(95.0)
        assert pct["p99"] == pytest.approx(99.0)
        assert pct["max"] == pytest.approx(100.0)

    def test_all_same_values(self) -> None:
        pct = compute_percentiles([7.0] * 50)
        assert pct["p50"] == pytest.approx(7.0)
        assert pct["p95"] == pytest.approx(7.0)
        assert pct["p99"] == pytest.approx(7.0)
        assert pct["max"] == pytest.approx(7.0)

    def test_keys_present(self) -> None:
        pct = compute_percentiles([1.0, 2.0, 3.0])
        assert set(pct.keys()) == {"p50", "p95", "p99", "max"}


# ---------------------------------------------------------------------------
# TestBuildLoadTestReport
# ---------------------------------------------------------------------------


class TestBuildLoadTestReport:
    def test_all_successful(self) -> None:
        config = LoadTestConfig()
        results = [
            _make_result(duration_ms=5.0, timestamp=100.0),
            _make_result(duration_ms=10.0, timestamp=100.5),
            _make_result(duration_ms=15.0, timestamp=101.0),
        ]
        report = build_load_test_report(config, results)

        assert report.total_requests == 3
        assert report.successful == 3
        assert report.failed == 0
        assert report.errors == {}
        assert report.max_ms == pytest.approx(15.0)

    def test_mixed_success_and_failure(self) -> None:
        config = LoadTestConfig()
        results = [
            _make_result(status_code=200, duration_ms=5.0, timestamp=1.0),
            _make_result(status_code=500, duration_ms=50.0, error="Internal Server Error", timestamp=2.0),
            _make_result(status_code=200, duration_ms=8.0, timestamp=3.0),
            _make_result(status_code=0, duration_ms=1000.0, error="Connection refused", timestamp=4.0),
        ]
        report = build_load_test_report(config, results)

        assert report.total_requests == 4
        assert report.successful == 2
        assert report.failed == 2
        assert report.errors == {"Internal Server Error": 1, "Connection refused": 1}

    def test_empty_results(self) -> None:
        config = LoadTestConfig()
        report = build_load_test_report(config, [])

        assert report.total_requests == 0
        assert report.successful == 0
        assert report.failed == 0
        assert report.requests_per_second == pytest.approx(0.0)
        assert report.p50_ms == pytest.approx(0.0)

    def test_single_result(self) -> None:
        config = LoadTestConfig()
        results = [_make_result(duration_ms=42.0)]
        report = build_load_test_report(config, results)

        assert report.total_requests == 1
        assert report.requests_per_second == pytest.approx(1.0)
        assert report.p50_ms == pytest.approx(42.0)

    def test_rps_calculation(self) -> None:
        config = LoadTestConfig()
        results = [
            _make_result(timestamp=10.0),
            _make_result(timestamp=12.0),
            _make_result(timestamp=14.0),
            _make_result(timestamp=16.0),
            _make_result(timestamp=18.0),
            _make_result(timestamp=20.0),
        ]
        report = build_load_test_report(config, results)

        # 6 requests across 10 seconds (20 - 10) = 0.6 rps
        assert report.requests_per_second == pytest.approx(0.6)

    def test_config_preserved(self) -> None:
        config = LoadTestConfig(target_url="http://test:9999", concurrent_agents=10)
        report = build_load_test_report(config, [])
        assert report.config is config

    def test_error_with_2xx_counts_as_failed(self) -> None:
        """A 200 with an error string is still counted as failed."""
        config = LoadTestConfig()
        results = [
            _make_result(status_code=200, error="timeout after response"),
        ]
        report = build_load_test_report(config, results)
        assert report.successful == 0
        assert report.failed == 1


# ---------------------------------------------------------------------------
# TestCheckThresholds
# ---------------------------------------------------------------------------


class TestCheckThresholds:
    def test_passing_thresholds(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=100,
            successful=100,
            failed=0,
            p50_ms=5.0,
            p95_ms=20.0,
            p99_ms=50.0,
            max_ms=80.0,
            requests_per_second=100.0,
            errors={},
        )
        violations = check_thresholds(report)
        assert violations == []

    def test_p99_violation(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=100,
            successful=100,
            failed=0,
            p50_ms=5.0,
            p95_ms=80.0,
            p99_ms=150.0,
            max_ms=200.0,
            requests_per_second=100.0,
            errors={},
        )
        violations = check_thresholds(report)
        assert len(violations) == 1
        assert "p99 latency" in violations[0]
        assert "150.0 ms" in violations[0]

    def test_error_rate_violation(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=100,
            successful=90,
            failed=10,
            p50_ms=5.0,
            p95_ms=20.0,
            p99_ms=50.0,
            max_ms=80.0,
            requests_per_second=100.0,
            errors={"timeout": 10},
        )
        violations = check_thresholds(report)
        assert len(violations) == 1
        assert "error rate" in violations[0]

    def test_both_violations(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=100,
            successful=80,
            failed=20,
            p50_ms=50.0,
            p95_ms=200.0,
            p99_ms=500.0,
            max_ms=1000.0,
            requests_per_second=10.0,
            errors={"conn refused": 20},
        )
        violations = check_thresholds(report)
        assert len(violations) == 2

    def test_custom_p99_limit(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=100,
            successful=100,
            failed=0,
            p50_ms=5.0,
            p95_ms=20.0,
            p99_ms=50.0,
            max_ms=80.0,
            requests_per_second=100.0,
            errors={},
        )
        # Tighter limit should trigger violation.
        violations = check_thresholds(report, p99_limit_ms=30.0)
        assert len(violations) == 1
        assert "50.0 ms" in violations[0]
        assert "30.0 ms" in violations[0]

    def test_exactly_at_threshold_passes(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=100,
            successful=95,
            failed=5,
            p50_ms=5.0,
            p95_ms=20.0,
            p99_ms=100.0,
            max_ms=100.0,
            requests_per_second=100.0,
            errors={"x": 5},
        )
        # p99 == limit is not a violation (must exceed), error rate 5% == 0.05 not > 0.05
        violations = check_thresholds(report, p99_limit_ms=100.0)
        assert violations == []

    def test_zero_requests_no_violations(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=0,
            successful=0,
            failed=0,
            p50_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            max_ms=0.0,
            requests_per_second=0.0,
            errors={},
        )
        violations = check_thresholds(report)
        assert violations == []


# ---------------------------------------------------------------------------
# TestFormatLoadTestReport
# ---------------------------------------------------------------------------


class TestFormatLoadTestReport:
    def test_pass_verdict(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=100,
            successful=100,
            failed=0,
            p50_ms=5.0,
            p95_ms=20.0,
            p99_ms=50.0,
            max_ms=80.0,
            requests_per_second=200.0,
            errors={},
        )
        text = format_load_test_report(report)
        assert "[PASS]" in text
        assert "FAIL" not in text

    def test_fail_verdict(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=100,
            successful=100,
            failed=0,
            p50_ms=5.0,
            p95_ms=80.0,
            p99_ms=150.0,
            max_ms=200.0,
            requests_per_second=100.0,
            errors={},
        )
        text = format_load_test_report(report)
        assert "[FAIL]" in text

    def test_contains_config_details(self) -> None:
        config = LoadTestConfig(
            target_url="http://example:9999",
            concurrent_agents=42,
        )
        report = LoadTestReport(
            config=config,
            total_requests=0,
            successful=0,
            failed=0,
            p50_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            max_ms=0.0,
            requests_per_second=0.0,
            errors={},
        )
        text = format_load_test_report(report)
        assert "http://example:9999" in text
        assert "42" in text

    def test_contains_latency_values(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=10,
            successful=10,
            failed=0,
            p50_ms=3.14,
            p95_ms=15.92,
            p99_ms=65.35,
            max_ms=89.79,
            requests_per_second=50.0,
            errors={},
        )
        text = format_load_test_report(report)
        assert "3.14" in text
        assert "15.92" in text
        assert "65.35" in text
        assert "89.79" in text

    def test_errors_section_present(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=10,
            successful=8,
            failed=2,
            p50_ms=5.0,
            p95_ms=20.0,
            p99_ms=50.0,
            max_ms=80.0,
            requests_per_second=10.0,
            errors={"Connection refused": 2},
        )
        text = format_load_test_report(report)
        assert "Errors:" in text
        assert "Connection refused" in text
        assert "[2x]" in text

    def test_violations_section_present(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=100,
            successful=70,
            failed=30,
            p50_ms=50.0,
            p95_ms=200.0,
            p99_ms=500.0,
            max_ms=1000.0,
            requests_per_second=10.0,
            errors={"timeout": 30},
        )
        text = format_load_test_report(report)
        assert "Violations:" in text

    def test_returns_string(self) -> None:
        report = LoadTestReport(
            config=LoadTestConfig(),
            total_requests=0,
            successful=0,
            failed=0,
            p50_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            max_ms=0.0,
            requests_per_second=0.0,
            errors={},
        )
        result = format_load_test_report(report)
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# TestRequestResult / TestLoadTestConfig (frozen dataclass invariants)
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_config_defaults(self) -> None:
        config = LoadTestConfig()
        assert config.target_url == "http://localhost:8052"
        assert config.concurrent_agents == 100
        assert config.duration_seconds == pytest.approx(30.0)
        assert config.requests_per_agent == 50
        assert config.endpoints == ["/status", "/tasks", "/health"]

    def test_config_is_frozen(self) -> None:
        config = LoadTestConfig()
        try:
            config.concurrent_agents = 999  # type: ignore[misc]
        except AttributeError:
            pass
        else:
            raise AssertionError("LoadTestConfig should be frozen")

    def test_result_fields(self) -> None:
        r = RequestResult(
            endpoint="/status",
            status_code=200,
            duration_ms=12.5,
            error=None,
            timestamp=1000.0,
        )
        assert r.endpoint == "/status"
        assert r.status_code == 200
        assert r.duration_ms == pytest.approx(12.5)
        assert r.error is None
        assert r.timestamp == pytest.approx(1000.0)

    def test_result_is_frozen(self) -> None:
        r = _make_result()
        try:
            r.status_code = 500  # type: ignore[misc]
        except AttributeError:
            pass
        else:
            raise AssertionError("RequestResult should be frozen")
