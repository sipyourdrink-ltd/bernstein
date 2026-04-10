"""Lightweight load-testing harness for the Bernstein task server API.

Provides dataclasses for configuration and reporting, percentile computation,
and threshold checking — all built on stdlib and httpx (no locust dependency).

Usage::

    from bernstein.testing.load_test import (
        LoadTestConfig,
        LoadTestReport,
        build_load_test_report,
        check_thresholds,
        compute_percentiles,
        format_load_test_report,
    )

    config = LoadTestConfig(concurrent_agents=50, duration_seconds=10.0)
    results = await run_load_test(config)  # async driver (not in this module)
    report = build_load_test_report(config, results)
    print(format_load_test_report(report))
    violations = check_thresholds(report)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadTestConfig:
    """Immutable configuration for a load-test run.

    Args:
        target_url: Base URL of the task server.
        concurrent_agents: Number of simulated concurrent agents.
        duration_seconds: Maximum wall-clock duration of the test.
        requests_per_agent: Requests each agent sends before stopping.
        endpoints: URL paths to cycle through.
    """

    target_url: str = "http://localhost:8052"
    concurrent_agents: int = 100
    duration_seconds: float = 30.0
    requests_per_agent: int = 50
    endpoints: list[str] = field(default_factory=lambda: ["/status", "/tasks", "/health"])


# ---------------------------------------------------------------------------
# Result / Report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestResult:
    """Outcome of a single HTTP request issued during a load test.

    Args:
        endpoint: URL path that was requested.
        status_code: HTTP status code (0 when the request never completed).
        duration_ms: Wall-clock time in milliseconds.
        error: Error message if the request failed, else ``None``.
        timestamp: Unix timestamp when the request was initiated.
    """

    endpoint: str
    status_code: int
    duration_ms: float
    error: str | None
    timestamp: float


@dataclass(frozen=True)
class LoadTestReport:
    """Aggregated results from a completed load-test run.

    Args:
        config: The configuration used for this run.
        total_requests: Total number of requests issued.
        successful: Number of requests with 2xx status.
        failed: Number of non-2xx or errored requests.
        p50_ms: Median latency in milliseconds.
        p95_ms: 95th-percentile latency.
        p99_ms: 99th-percentile latency.
        max_ms: Maximum observed latency.
        requests_per_second: Throughput (total / elapsed wall-clock time).
        errors: Mapping of error message to occurrence count.
    """

    config: LoadTestConfig
    total_requests: int
    successful: int
    failed: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    requests_per_second: float
    errors: dict[str, int]


# ---------------------------------------------------------------------------
# Percentile computation
# ---------------------------------------------------------------------------


def compute_percentiles(durations: list[float]) -> dict[str, float]:
    """Compute p50, p95, p99, and max from a list of durations.

    Returns a dict with keys ``"p50"``, ``"p95"``, ``"p99"``, ``"max"``.
    Returns all zeros when *durations* is empty.

    Uses nearest-rank interpolation: for percentile *p* and *N* sorted
    values, the index is ``ceil(p / 100 * N) - 1``.
    """
    if not durations:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}

    sorted_d = sorted(durations)
    n = len(sorted_d)

    def _pct(p: float) -> float:
        idx = max(0, math.ceil(p / 100.0 * n) - 1)
        return sorted_d[idx]

    return {
        "p50": _pct(50),
        "p95": _pct(95),
        "p99": _pct(99),
        "max": sorted_d[-1],
    }


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def build_load_test_report(
    config: LoadTestConfig,
    results: list[RequestResult],
) -> LoadTestReport:
    """Build an aggregated ``LoadTestReport`` from raw request results.

    A request is counted as *successful* when its ``status_code`` is in the
    2xx range and ``error`` is ``None``.
    """
    successful = sum(1 for r in results if 200 <= r.status_code < 300 and r.error is None)
    failed = len(results) - successful

    durations = [r.duration_ms for r in results]
    pct = compute_percentiles(durations)

    # Compute throughput from wall-clock span of timestamps.
    if len(results) >= 2:
        earliest = min(r.timestamp for r in results)
        latest = max(r.timestamp for r in results)
        elapsed = latest - earliest
        rps = len(results) / elapsed if elapsed > 0 else 0.0
    elif len(results) == 1:
        rps = 1.0
    else:
        rps = 0.0

    # Tally errors by message.
    error_counts: dict[str, int] = {}
    for r in results:
        if r.error is not None:
            error_counts[r.error] = error_counts.get(r.error, 0) + 1

    return LoadTestReport(
        config=config,
        total_requests=len(results),
        successful=successful,
        failed=failed,
        p50_ms=pct["p50"],
        p95_ms=pct["p95"],
        p99_ms=pct["p99"],
        max_ms=pct["max"],
        requests_per_second=rps,
        errors=error_counts,
    )


# ---------------------------------------------------------------------------
# Threshold checking
# ---------------------------------------------------------------------------


def check_thresholds(
    report: LoadTestReport,
    p99_limit_ms: float = 100.0,
) -> list[str]:
    """Return a list of threshold violation messages.

    Checks:
    * p99 latency must be below *p99_limit_ms*.
    * Error rate must be below 5 %.

    Returns an empty list when all thresholds pass.
    """
    violations: list[str] = []

    if report.p99_ms > p99_limit_ms:
        violations.append(f"p99 latency {report.p99_ms:.1f} ms exceeds limit {p99_limit_ms:.1f} ms")

    if report.total_requests > 0:
        error_rate = report.failed / report.total_requests
        if error_rate > 0.05:
            violations.append(f"error rate {error_rate:.1%} exceeds 5 % threshold")

    return violations


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_load_test_report(report: LoadTestReport) -> str:
    """Format a ``LoadTestReport`` as a human-readable Rich-style string.

    Includes a PASS/FAIL verdict based on the p99 < 100 ms threshold.
    """
    violations = check_thresholds(report)
    verdict = "FAIL" if violations else "PASS"

    lines: list[str] = [
        "",
        f"  Load Test Report  [{verdict}]",
        "=" * 50,
        f"  Target URL        : {report.config.target_url}",
        f"  Concurrent agents : {report.config.concurrent_agents}",
        f"  Duration (config) : {report.config.duration_seconds:.1f} s",
        f"  Requests/agent    : {report.config.requests_per_agent}",
        f"  Endpoints         : {', '.join(report.config.endpoints)}",
        "-" * 50,
        f"  Total requests    : {report.total_requests}",
        f"  Successful        : {report.successful}",
        f"  Failed            : {report.failed}",
        f"  Requests/sec      : {report.requests_per_second:.1f}",
        "-" * 50,
        f"  p50  latency      : {report.p50_ms:.2f} ms",
        f"  p95  latency      : {report.p95_ms:.2f} ms",
        f"  p99  latency      : {report.p99_ms:.2f} ms",
        f"  max  latency      : {report.max_ms:.2f} ms",
    ]

    if report.errors:
        lines.append("-" * 50)
        lines.append("  Errors:")
        for msg, count in sorted(report.errors.items()):
            lines.append(f"    [{count}x] {msg}")

    if violations:
        lines.append("-" * 50)
        lines.append("  Violations:")
        for v in violations:
            lines.append(f"    - {v}")

    lines.append("=" * 50)
    lines.append("")

    return "\n".join(lines)
