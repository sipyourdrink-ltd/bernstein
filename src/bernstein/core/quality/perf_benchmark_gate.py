"""Performance benchmark gate that rejects changes causing >10% regression.

Runs arbitrary shell commands multiple times, collects wall-clock time and
peak memory usage, then compares before/after results to detect regressions
that exceed a configurable threshold.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Data models
# ------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkResult:
    """Metrics captured from running a single benchmark command.

    Args:
        name: Human-readable benchmark identifier.
        wall_clock_ms: Mean wall-clock time in milliseconds.
        peak_memory_mb: Peak RSS in megabytes (None when unavailable).
        throughput: Operations per second (None when not measured).
        iterations: Number of iterations executed.
    """

    name: str
    wall_clock_ms: float
    peak_memory_mb: float | None
    throughput: float | None
    iterations: int


@dataclass(frozen=True)
class RegressionResult:
    """Comparison outcome for a single benchmark.

    Args:
        benchmark_name: Name of the benchmark being compared.
        before: Baseline measurement.
        after: Current measurement.
        wall_clock_delta_pct: Percentage change in wall-clock time.
        memory_delta_pct: Percentage change in peak memory (None when
            memory data is unavailable).
        regressed: Whether the change exceeds the threshold.
        threshold_pct: The threshold that was applied.
    """

    benchmark_name: str
    before: BenchmarkResult
    after: BenchmarkResult
    wall_clock_delta_pct: float
    memory_delta_pct: float | None
    regressed: bool
    threshold_pct: float


@dataclass(frozen=True)
class BenchmarkGateResult:
    """Aggregate outcome of the performance benchmark gate.

    Args:
        passed: True when no benchmark regressed beyond threshold.
        results: Per-benchmark comparison results.
        summary: Human-readable summary string.
    """

    passed: bool
    results: list[RegressionResult] = field(default_factory=lambda: list[RegressionResult]())
    summary: str = ""


# ------------------------------------------------------------------
# Benchmark runner
# ------------------------------------------------------------------


def _get_peak_memory_mb() -> float | None:
    """Return peak RSS of the current process's children in MB.

    Uses ``resource.getrusage`` which is available on POSIX systems.
    Returns None on platforms where it is unavailable.
    """
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        ru_maxrss = usage.ru_maxrss
        if ru_maxrss <= 0:
            return None
        # macOS reports bytes, Linux reports kilobytes
        import sys

        if sys.platform == "darwin":
            return ru_maxrss / (1024.0 * 1024.0)
        return ru_maxrss / 1024.0
    except (ImportError, OSError):
        return None


def run_benchmark(
    command: str,
    *,
    name: str | None = None,
    iterations: int = 3,
) -> BenchmarkResult:
    """Run a shell command multiple times and collect performance stats.

    Args:
        command: Shell command to benchmark.
        name: Benchmark identifier (defaults to the command string).
        iterations: Number of times to run the command.

    Returns:
        A ``BenchmarkResult`` with averaged wall-clock time, best-observed
        peak memory, and derived throughput.

    Raises:
        RuntimeError: When the command fails on any iteration.
        ValueError: When iterations is less than 1.
    """
    if iterations < 1:
        raise ValueError(f"iterations must be >= 1, got {iterations}")

    bench_name = name or command
    elapsed_times: list[float] = []
    peak_memories: list[float] = []

    for i in range(iterations):
        logger.debug("Benchmark %r iteration %d/%d", bench_name, i + 1, iterations)

        start = time.monotonic()
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
        )
        elapsed_ms = (time.monotonic() - start) * 1000.0

        if result.returncode != 0:
            combined = (result.stdout + result.stderr).strip()
            raise RuntimeError(f"Benchmark {bench_name!r} failed on iteration {i + 1}: {combined}")

        elapsed_times.append(elapsed_ms)

        mem = _get_peak_memory_mb()
        if mem is not None:
            peak_memories.append(mem)

    mean_ms = sum(elapsed_times) / len(elapsed_times)
    peak_memory_mb = max(peak_memories) if peak_memories else None
    throughput = (1000.0 / mean_ms) if mean_ms > 0 else None

    return BenchmarkResult(
        name=bench_name,
        wall_clock_ms=round(mean_ms, 3),
        peak_memory_mb=round(peak_memory_mb, 3) if peak_memory_mb is not None else None,
        throughput=round(throughput, 3) if throughput is not None else None,
        iterations=iterations,
    )


# ------------------------------------------------------------------
# Comparison
# ------------------------------------------------------------------


def compare_benchmarks(
    before: BenchmarkResult,
    after: BenchmarkResult,
    *,
    threshold: float = 10.0,
) -> RegressionResult:
    """Compare two benchmark results and detect regression.

    A regression is detected when wall-clock time increases by more than
    ``threshold`` percent **or** peak memory increases by more than
    ``threshold`` percent.

    Args:
        before: Baseline measurement.
        after: Current measurement.
        threshold: Maximum acceptable percentage increase (e.g. 10.0 for 10%).

    Returns:
        A ``RegressionResult`` describing the comparison.
    """
    if before.wall_clock_ms > 0:
        wall_delta_pct = ((after.wall_clock_ms - before.wall_clock_ms) / before.wall_clock_ms) * 100.0
    else:
        wall_delta_pct = 0.0

    memory_delta_pct: float | None = None
    if before.peak_memory_mb is not None and after.peak_memory_mb is not None and before.peak_memory_mb > 0:
        memory_delta_pct = ((after.peak_memory_mb - before.peak_memory_mb) / before.peak_memory_mb) * 100.0

    regressed = wall_delta_pct > threshold
    if memory_delta_pct is not None and memory_delta_pct > threshold:
        regressed = True

    return RegressionResult(
        benchmark_name=after.name,
        before=before,
        after=after,
        wall_clock_delta_pct=round(wall_delta_pct, 2),
        memory_delta_pct=round(memory_delta_pct, 2) if memory_delta_pct is not None else None,
        regressed=regressed,
        threshold_pct=threshold,
    )


# ------------------------------------------------------------------
# Gate runner
# ------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkSpec:
    """Configuration for a single benchmark to run.

    Args:
        command: Shell command to execute.
        name: Human-readable identifier.
        iterations: Number of times to run.
    """

    command: str
    name: str
    iterations: int = 3


def run_benchmark_gate(
    benchmarks: Sequence[BenchmarkSpec],
    *,
    before_results: Sequence[BenchmarkResult] | None = None,
    threshold: float = 10.0,
) -> BenchmarkGateResult:
    """Run configured benchmarks and detect regressions.

    If ``before_results`` is provided, it is used as the baseline for
    comparison.  Otherwise, each benchmark is run once for the baseline
    (``before``) and once for the current state (``after``).

    Args:
        benchmarks: Benchmark specifications to run.
        before_results: Optional pre-computed baseline results.
        threshold: Maximum acceptable percentage regression.

    Returns:
        A ``BenchmarkGateResult`` indicating whether the gate passed.
    """
    if not benchmarks:
        return BenchmarkGateResult(
            passed=True,
            results=[],
            summary="No benchmarks configured.",
        )

    comparisons: list[RegressionResult] = []

    before_map: dict[str, BenchmarkResult] = {}
    if before_results is not None:
        for br in before_results:
            before_map[br.name] = br

    for spec in benchmarks:
        logger.info("Running benchmark %r", spec.name)
        after = run_benchmark(spec.command, name=spec.name, iterations=spec.iterations)

        before = before_map.get(spec.name)
        if before is None:
            before = run_benchmark(spec.command, name=spec.name, iterations=spec.iterations)

        comparison = compare_benchmarks(before, after, threshold=threshold)
        comparisons.append(comparison)

    regressions = [c for c in comparisons if c.regressed]
    passed = len(regressions) == 0

    if passed:
        summary = f"All {len(comparisons)} benchmark(s) within {threshold:.1f}% regression threshold."
    else:
        lines = [f"Performance regression detected ({len(regressions)}/{len(comparisons)} benchmarks):"]
        for reg in regressions:
            parts = [f"  {reg.benchmark_name}: wall_clock +{reg.wall_clock_delta_pct:.1f}%"]
            if reg.memory_delta_pct is not None:
                parts.append(f"memory +{reg.memory_delta_pct:.1f}%")
            lines.append(", ".join(parts))
        summary = "\n".join(lines)

    return BenchmarkGateResult(
        passed=passed,
        results=comparisons,
        summary=summary,
    )
