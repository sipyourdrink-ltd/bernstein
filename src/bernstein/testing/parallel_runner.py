"""Parallel test execution runner — bucketed file-level isolation with history-aware balancing.

Distributes test files across worker buckets for parallel execution while
maintaining per-file isolation (each file runs in its own subprocess) to
prevent memory leaks. Supports historical duration data for smarter
bucket balancing and provides a structured report with speedup metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class TestBucket:
    """A bucket of test files assigned to a single worker."""

    __test__ = False  # Not a pytest test class

    bucket_id: int
    test_files: list[str]
    estimated_duration_s: float


@dataclass(frozen=True)
class ParallelConfig:
    """Configuration for parallel test execution."""

    max_workers: int = 8
    timeout_per_file: int = 120
    isolation_mode: Literal["file", "directory"] = "file"
    fail_fast: bool = True


@dataclass(frozen=True)
class FileTestResult:
    """Result from running a single test file."""

    file_path: str
    passed: int
    failed: int
    skipped: int
    duration_s: float
    exit_code: int
    error: str | None


@dataclass(frozen=True)
class ParallelRunReport:
    """Aggregate report from a parallel test run."""

    total_files: int
    total_passed: int
    total_failed: int
    total_skipped: int
    wall_time_s: float
    cpu_time_s: float
    speedup: float
    results: list[FileTestResult] = field(default_factory=lambda: list[FileTestResult]())
    failures: list[str] = field(default_factory=lambda: list[str]())


def discover_test_files(test_dir: Path, pattern: str = "test_*.py") -> list[str]:
    """Find test files matching a glob pattern under the given directory.

    Args:
        test_dir: Root directory to search for test files.
        pattern: Glob pattern for test file names.

    Returns:
        Sorted list of test file paths as strings.
    """
    if not test_dir.is_dir():
        return []
    return sorted(str(p) for p in test_dir.glob(pattern))


def bucket_tests(
    files: list[str],
    num_buckets: int,
    history: dict[str, float] | None = None,
) -> list[TestBucket]:
    """Distribute test files across buckets for parallel execution.

    Uses a greedy longest-processing-time-first algorithm: files are sorted
    by estimated duration (descending) and each file is assigned to the
    bucket with the smallest current total. This produces near-optimal
    load balancing.

    Args:
        files: List of test file paths.
        num_buckets: Number of buckets (workers) to distribute across.
        history: Optional mapping of file path to historical duration in
            seconds. Files without history are assigned a default of 1.0s.

    Returns:
        List of TestBucket instances, one per bucket.
    """
    if num_buckets < 1:
        num_buckets = 1

    if not files:
        return [TestBucket(bucket_id=i, test_files=[], estimated_duration_s=0.0) for i in range(num_buckets)]

    effective_history = history or {}
    default_duration = 1.0

    # Build (file, duration) pairs and sort descending by duration (LPT)
    timed_files = [(f, effective_history.get(f, default_duration)) for f in files]
    timed_files.sort(key=lambda x: x[1], reverse=True)

    # Initialize buckets
    bucket_files: list[list[str]] = [[] for _ in range(num_buckets)]
    bucket_durations: list[float] = [0.0] * num_buckets

    # Greedy assignment: always add to the lightest bucket
    for file_path, duration in timed_files:
        lightest = min(range(num_buckets), key=lambda i: bucket_durations[i])
        bucket_files[lightest].append(file_path)
        bucket_durations[lightest] += duration

    return [
        TestBucket(
            bucket_id=i,
            test_files=bucket_files[i],
            estimated_duration_s=round(bucket_durations[i], 3),
        )
        for i in range(num_buckets)
    ]


def build_parallel_report(
    results: list[FileTestResult],
    wall_time: float,
) -> ParallelRunReport:
    """Build an aggregate report from individual file results.

    Args:
        results: List of per-file test results.
        wall_time: Total elapsed wall-clock time in seconds.

    Returns:
        A ParallelRunReport with aggregated counts and speedup ratio.
    """
    total_passed = sum(r.passed for r in results)
    total_failed = sum(r.failed for r in results)
    total_skipped = sum(r.skipped for r in results)
    cpu_time = sum(r.duration_s for r in results)

    failures = [r.file_path for r in results if r.exit_code != 0]

    safe_wall = max(wall_time, 0.001)
    speedup = cpu_time / safe_wall

    return ParallelRunReport(
        total_files=len(results),
        total_passed=total_passed,
        total_failed=total_failed,
        total_skipped=total_skipped,
        wall_time_s=round(wall_time, 3),
        cpu_time_s=round(cpu_time, 3),
        speedup=round(speedup, 2),
        results=list(results),
        failures=failures,
    )


def format_parallel_report(report: ParallelRunReport) -> str:
    """Format a parallel run report as a human-readable summary.

    Args:
        report: The aggregate report to format.

    Returns:
        Multi-line string with test counts, timing, speedup, and failures.
    """
    lines: list[str] = []
    sep = "=" * 60

    lines.append(sep)
    lines.append("Parallel Test Run Report")
    lines.append(sep)
    lines.append(f"Files:   {report.total_files}")
    lines.append(
        f"Passed:  {report.total_passed}  |  Failed: {report.total_failed}  |  Skipped: {report.total_skipped}"
    )
    lines.append(f"Wall:    {report.wall_time_s:.1f}s")
    lines.append(f"CPU:     {report.cpu_time_s:.1f}s")
    lines.append(f"Speedup: {report.speedup:.2f}x")

    if report.failures:
        lines.append(sep)
        lines.append(f"FAILURES ({len(report.failures)}):")
        for path in report.failures:
            lines.append(f"  - {path}")

    lines.append(sep)
    return "\n".join(lines)
