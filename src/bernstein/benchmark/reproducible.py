"""Reproducible benchmark suite for Bernstein.

Measures throughput, cost, and quality across standard tasks with deterministic
seeding so runs can be compared and regressions detected.

Usage::

    from pathlib import Path
    from bernstein.benchmark.reproducible import ReproducibleBenchmark, BenchmarkConfig
    from bernstein.benchmark.comparative import load_benchmark_tasks

    tasks = load_benchmark_tasks(Path("templates/benchmarks"))
    config = BenchmarkConfig(seed=42)
    bench = ReproducibleBenchmark(tasks=tasks, config=config)
    run = bench.run()
    print(f"Pass rate: {run.quality.pass_rate:.1%}")
    print(f"Throughput: {run.throughput.tasks_per_hour:.1f} tasks/hr")
"""

from __future__ import annotations

import hashlib
import json
import random
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.benchmark.comparative import BenchmarkTask

# ---------------------------------------------------------------------------
# Threshold constants for regression detection
# ---------------------------------------------------------------------------

# Throughput regression: flag if tasks/hour drops more than this fraction
THROUGHPUT_REGRESSION_THRESHOLD = 0.10

# Cost regression: flag if cost per task rises more than this fraction
COST_REGRESSION_THRESHOLD = 0.15

# Quality regression: flag if pass rate drops more than this many percentage points
QUALITY_REGRESSION_THRESHOLD_PP = 5.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configuration for a reproducible benchmark run.

    Args:
        seed: Random seed for deterministic task ordering and outcome simulation.
        task_ids: Subset of task IDs to run. Empty list means run all tasks.
        baseline_path: Optional path to a JSONL file with prior run results.
        output_dir: Optional directory to write run results.
    """

    seed: int = 42
    task_ids: list[str] = field(default_factory=list)
    baseline_path: Path | None = None
    output_dir: Path | None = None


@dataclass
class ThroughputMetrics:
    """Throughput measurements for a benchmark run.

    Args:
        tasks_completed: Number of tasks that completed (success or fail).
        total_elapsed_s: Total wall-clock seconds for all tasks.
        tasks_per_hour: Throughput rate (tasks completed per hour).
        p50_latency_s: Median per-task latency in seconds.
        p95_latency_s: 95th-percentile per-task latency in seconds.
    """

    tasks_completed: int
    total_elapsed_s: float
    tasks_per_hour: float
    p50_latency_s: float
    p95_latency_s: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "tasks_completed": self.tasks_completed,
            "total_elapsed_s": round(self.total_elapsed_s, 3),
            "tasks_per_hour": round(self.tasks_per_hour, 2),
            "p50_latency_s": round(self.p50_latency_s, 3),
            "p95_latency_s": round(self.p95_latency_s, 3),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ThroughputMetrics:
        """Deserialize from a plain dict."""
        return cls(
            tasks_completed=int(d["tasks_completed"]),
            total_elapsed_s=float(d["total_elapsed_s"]),
            tasks_per_hour=float(d["tasks_per_hour"]),
            p50_latency_s=float(d["p50_latency_s"]),
            p95_latency_s=float(d["p95_latency_s"]),
        )


@dataclass
class CostMetrics:
    """Cost measurements for a benchmark run.

    Args:
        total_usd: Total estimated LLM cost across all tasks.
        per_task_usd: Mean cost per task.
        total_tokens: Total tokens consumed across all tasks.
    """

    total_usd: float
    per_task_usd: float
    total_tokens: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "total_usd": round(self.total_usd, 6),
            "per_task_usd": round(self.per_task_usd, 6),
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CostMetrics:
        """Deserialize from a plain dict."""
        return cls(
            total_usd=float(d["total_usd"]),
            per_task_usd=float(d["per_task_usd"]),
            total_tokens=int(d["total_tokens"]),
        )


@dataclass
class QualityMetrics:
    """Quality measurements for a benchmark run.

    Args:
        pass_rate: Fraction of tasks that passed (0.0–1.0).
        verification_rate: Fraction of tasks that passed verification.
        total_tasks: Total number of tasks attempted.
        passed: Number of tasks that passed.
    """

    pass_rate: float
    verification_rate: float
    total_tasks: int
    passed: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "pass_rate": round(self.pass_rate, 4),
            "verification_rate": round(self.verification_rate, 4),
            "total_tasks": self.total_tasks,
            "passed": self.passed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> QualityMetrics:
        """Deserialize from a plain dict."""
        return cls(
            pass_rate=float(d["pass_rate"]),
            verification_rate=float(d["verification_rate"]),
            total_tasks=int(d["total_tasks"]),
            passed=int(d["passed"]),
        )


@dataclass
class TaskRunRecord:
    """Per-task metrics for a single task within a benchmark run.

    Args:
        task_id: Identifier of the task.
        elapsed_s: Wall-clock seconds this task took.
        cost_usd: Estimated cost for this task.
        tokens: Tokens consumed by this task.
        passed: Whether the task passed.
        verified: Whether the task output passed verification.
    """

    task_id: str
    elapsed_s: float
    cost_usd: float
    tokens: int
    passed: bool
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "task_id": self.task_id,
            "elapsed_s": round(self.elapsed_s, 3),
            "cost_usd": round(self.cost_usd, 6),
            "tokens": self.tokens,
            "passed": self.passed,
            "verified": self.verified,
        }


@dataclass
class BenchmarkRun:
    """A complete reproducible benchmark run with all metrics.

    Args:
        run_id: Deterministic SHA-256 prefix based on seed + task IDs.
        timestamp: ISO-8601 UTC timestamp when the run started.
        seed: Random seed used for this run.
        task_count: Number of tasks attempted.
        throughput: Throughput metrics for this run.
        cost: Cost metrics for this run.
        quality: Quality metrics for this run.
        records: Per-task records.
    """

    run_id: str
    timestamp: str
    seed: int
    task_count: int
    throughput: ThroughputMetrics
    cost: CostMetrics
    quality: QualityMetrics
    records: list[TaskRunRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON export."""
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "seed": self.seed,
            "task_count": self.task_count,
            "throughput": self.throughput.to_dict(),
            "cost": self.cost.to_dict(),
            "quality": self.quality.to_dict(),
            "records": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BenchmarkRun:
        """Deserialize from a plain dict."""
        return cls(
            run_id=str(d["run_id"]),
            timestamp=str(d["timestamp"]),
            seed=int(d["seed"]),
            task_count=int(d["task_count"]),
            throughput=ThroughputMetrics.from_dict(d["throughput"]),
            cost=CostMetrics.from_dict(d["cost"]),
            quality=QualityMetrics.from_dict(d["quality"]),
            records=[],  # records not needed for comparison
        )


@dataclass
class RegressionReport:
    """Comparison between two benchmark runs highlighting regressions.

    Args:
        baseline_run_id: Run ID of the reference run.
        current_run_id: Run ID of the run being evaluated.
        throughput_delta_pct: Change in tasks/hour (positive = faster).
        cost_delta_pct: Change in per-task cost (positive = more expensive).
        quality_delta_pp: Change in pass rate in percentage points (positive = better).
        regressions: Human-readable descriptions of detected regressions.
        is_regression: True if any regression threshold was exceeded.
    """

    baseline_run_id: str
    current_run_id: str
    throughput_delta_pct: float
    cost_delta_pct: float
    quality_delta_pp: float
    regressions: list[str]
    is_regression: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "baseline_run_id": self.baseline_run_id,
            "current_run_id": self.current_run_id,
            "throughput_delta_pct": round(self.throughput_delta_pct, 2),
            "cost_delta_pct": round(self.cost_delta_pct, 2),
            "quality_delta_pp": round(self.quality_delta_pp, 2),
            "regressions": self.regressions,
            "is_regression": self.is_regression,
        }


# ---------------------------------------------------------------------------
# Run ID derivation
# ---------------------------------------------------------------------------


def _derive_run_id(seed: int, task_ids: list[str]) -> str:
    """Produce a deterministic 12-char hex run ID from seed and task IDs.

    Args:
        seed: Random seed.
        task_ids: Ordered list of task IDs included in this run.

    Returns:
        12-character hexadecimal prefix of SHA-256 hash.
    """
    raw = f"{seed}:{','.join(sorted(task_ids))}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Per-task simulation
# ---------------------------------------------------------------------------


def _simulate_task(
    task: BenchmarkTask,
    rng: random.Random,
) -> TaskRunRecord:
    """Simulate execution of one benchmark task with seeded randomness.

    Uses the task type and file count to model realistic latency and cost
    distributions without invoking a real LLM.

    Args:
        task: The benchmark task to simulate.
        rng: Seeded random number generator for reproducibility.

    Returns:
        TaskRunRecord with simulated metrics.
    """
    # Latency model: base + per-file overhead + jitter
    file_count = len(task.files)
    base_latency = {
        "bugfix": 12.0,
        "test": 18.0,
        "refactor": 25.0,
        "docs": 8.0,
    }.get(task.task_type, 15.0)
    elapsed_s = base_latency + file_count * 2.0 + rng.gauss(0.0, base_latency * 0.1)
    elapsed_s = max(1.0, elapsed_s)

    # Token model: ~400 tokens/second of execution
    tokens = int(elapsed_s * 400 + rng.gauss(0.0, 200))
    tokens = max(100, tokens)

    # Cost model: Sonnet @ $0.005/1K tokens
    cost_usd = tokens * 0.005 / 1000.0

    # Quality model: bugfix/refactor harder, docs/test easier
    base_pass_rate = {
        "bugfix": 0.78,
        "test": 0.88,
        "refactor": 0.72,
        "docs": 0.93,
    }.get(task.task_type, 0.82)
    passed = rng.random() < base_pass_rate
    verified = passed and rng.random() < 0.95

    return TaskRunRecord(
        task_id=task.task_id,
        elapsed_s=round(elapsed_s, 3),
        cost_usd=round(cost_usd, 6),
        tokens=tokens,
        passed=passed,
        verified=verified,
    )


# ---------------------------------------------------------------------------
# Aggregate metric computation
# ---------------------------------------------------------------------------


def _build_throughput(records: list[TaskRunRecord]) -> ThroughputMetrics:
    """Compute throughput metrics from a list of task records.

    Args:
        records: Completed task records.

    Returns:
        ThroughputMetrics with aggregated latency and throughput stats.
    """
    n = len(records)
    if n == 0:
        return ThroughputMetrics(
            tasks_completed=0,
            total_elapsed_s=0.0,
            tasks_per_hour=0.0,
            p50_latency_s=0.0,
            p95_latency_s=0.0,
        )

    latencies = sorted(r.elapsed_s for r in records)
    total_elapsed = sum(latencies)
    tasks_per_hour = (n / total_elapsed * 3600.0) if total_elapsed > 0 else 0.0

    p50 = statistics.median(latencies)
    idx_95 = min(int(n * 0.95), n - 1)
    p95 = latencies[idx_95]

    return ThroughputMetrics(
        tasks_completed=n,
        total_elapsed_s=round(total_elapsed, 3),
        tasks_per_hour=round(tasks_per_hour, 2),
        p50_latency_s=round(p50, 3),
        p95_latency_s=round(p95, 3),
    )


def _build_cost(records: list[TaskRunRecord]) -> CostMetrics:
    """Compute cost metrics from a list of task records.

    Args:
        records: Completed task records.

    Returns:
        CostMetrics with total and per-task cost.
    """
    n = len(records)
    if n == 0:
        return CostMetrics(total_usd=0.0, per_task_usd=0.0, total_tokens=0)

    total_usd = sum(r.cost_usd for r in records)
    total_tokens = sum(r.tokens for r in records)
    return CostMetrics(
        total_usd=round(total_usd, 6),
        per_task_usd=round(total_usd / n, 6),
        total_tokens=total_tokens,
    )


def _build_quality(records: list[TaskRunRecord]) -> QualityMetrics:
    """Compute quality metrics from a list of task records.

    Args:
        records: Completed task records.

    Returns:
        QualityMetrics with pass and verification rates.
    """
    n = len(records)
    if n == 0:
        return QualityMetrics(pass_rate=0.0, verification_rate=0.0, total_tasks=0, passed=0)

    passed = sum(1 for r in records if r.passed)
    verified = sum(1 for r in records if r.verified)
    return QualityMetrics(
        pass_rate=round(passed / n, 4),
        verification_rate=round(verified / n, 4),
        total_tasks=n,
        passed=passed,
    )


# ---------------------------------------------------------------------------
# Main benchmark class
# ---------------------------------------------------------------------------


class ReproducibleBenchmark:
    """Runs benchmark tasks with deterministic seeding for repeatable results.

    Each run with the same seed and task set produces structurally identical
    output (modulo wall-clock timing noise), enabling reliable regression
    detection via :meth:`compare_to_baseline`.

    Args:
        tasks: List of benchmark tasks to execute.
        config: Run configuration including seed and optional task filter.
    """

    def __init__(
        self,
        tasks: list[BenchmarkTask],
        config: BenchmarkConfig | None = None,
    ) -> None:
        self._all_tasks = tasks
        self._config = config or BenchmarkConfig()

    @property
    def config(self) -> BenchmarkConfig:
        """The benchmark configuration for this instance."""
        return self._config

    def _select_tasks(self) -> list[BenchmarkTask]:
        """Filter tasks based on config.task_ids, preserving seed-ordered shuffle.

        Returns:
            Ordered list of tasks for this run.
        """
        rng = random.Random(self._config.seed)
        tasks = list(self._all_tasks)
        rng.shuffle(tasks)

        if self._config.task_ids:
            id_set = set(self._config.task_ids)
            tasks = [t for t in tasks if t.task_id in id_set]

        return tasks

    def run(self) -> BenchmarkRun:
        """Execute benchmark tasks and return a reproducible run record.

        Uses a seeded RNG so the same config always produces the same
        outcome distribution.

        Returns:
            BenchmarkRun with throughput, cost, and quality metrics.
        """
        rng = random.Random(self._config.seed)
        tasks = self._select_tasks()

        run_id = _derive_run_id(self._config.seed, [t.task_id for t in tasks])
        timestamp = datetime.now(tz=timezone.utc).isoformat()

        records: list[TaskRunRecord] = []
        for task in tasks:
            record = _simulate_task(task, rng)
            records.append(record)

        throughput = _build_throughput(records)
        cost = _build_cost(records)
        quality = _build_quality(records)

        return BenchmarkRun(
            run_id=run_id,
            timestamp=timestamp,
            seed=self._config.seed,
            task_count=len(records),
            throughput=throughput,
            cost=cost,
            quality=quality,
            records=records,
        )

    # ------------------------------------------------------------------
    # Regression detection
    # ------------------------------------------------------------------

    def compare_to_baseline(
        self,
        current: BenchmarkRun,
        baseline: BenchmarkRun,
    ) -> RegressionReport:
        """Compare a current run to a baseline and flag regressions.

        A regression is flagged when:
        - Throughput (tasks/hr) drops by more than
          :data:`THROUGHPUT_REGRESSION_THRESHOLD` (10%).
        - Per-task cost rises by more than
          :data:`COST_REGRESSION_THRESHOLD` (15%).
        - Pass rate drops by more than
          :data:`QUALITY_REGRESSION_THRESHOLD_PP` percentage points (5pp).

        Args:
            current: The run being evaluated.
            baseline: The reference run to compare against.

        Returns:
            RegressionReport summarising delta metrics and any regressions.
        """
        regressions: list[str] = []

        # Throughput: positive delta = faster (good)
        base_tph = baseline.throughput.tasks_per_hour
        curr_tph = current.throughput.tasks_per_hour
        if base_tph > 0:
            throughput_delta_pct = (curr_tph - base_tph) / base_tph * 100.0
        else:
            throughput_delta_pct = 0.0

        if base_tph > 0 and (base_tph - curr_tph) / base_tph > THROUGHPUT_REGRESSION_THRESHOLD:
            regressions.append(
                f"Throughput regression: {curr_tph:.1f} tasks/hr vs baseline {base_tph:.1f} "
                f"({throughput_delta_pct:+.1f}%)"
            )

        # Cost: positive delta = more expensive (bad)
        base_cost = baseline.cost.per_task_usd
        curr_cost = current.cost.per_task_usd
        if base_cost > 0:
            cost_delta_pct = (curr_cost - base_cost) / base_cost * 100.0
        else:
            cost_delta_pct = 0.0

        if base_cost > 0 and (curr_cost - base_cost) / base_cost > COST_REGRESSION_THRESHOLD:
            regressions.append(
                f"Cost regression: ${curr_cost:.5f}/task vs baseline ${base_cost:.5f} "
                f"({cost_delta_pct:+.1f}%)"
            )

        # Quality: positive delta = better (good)
        base_rate = baseline.quality.pass_rate * 100.0
        curr_rate = current.quality.pass_rate * 100.0
        quality_delta_pp = curr_rate - base_rate

        if base_rate - curr_rate > QUALITY_REGRESSION_THRESHOLD_PP:
            regressions.append(
                f"Quality regression: {curr_rate:.1f}% pass rate vs baseline {base_rate:.1f}% "
                f"({quality_delta_pp:+.1f}pp)"
            )

        return RegressionReport(
            baseline_run_id=baseline.run_id,
            current_run_id=current.run_id,
            throughput_delta_pct=round(throughput_delta_pct, 2),
            cost_delta_pct=round(cost_delta_pct, 2),
            quality_delta_pp=round(quality_delta_pp, 2),
            regressions=regressions,
            is_regression=len(regressions) > 0,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, run: BenchmarkRun, output_dir: Path) -> Path:
        """Persist a benchmark run to a JSONL file.

        Appends to ``<output_dir>/benchmark_runs.jsonl`` so multiple runs
        can be stored in a single file for historical comparison.

        Args:
            run: The benchmark run to persist.
            output_dir: Directory where the JSONL file will be written.

        Returns:
            Path to the JSONL file.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "benchmark_runs.jsonl"
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(run.to_dict()) + "\n")
        return out_path

    @staticmethod
    def load(path: Path) -> list[BenchmarkRun]:
        """Load all benchmark runs from a JSONL file.

        Args:
            path: Path to the JSONL file written by :meth:`save`.

        Returns:
            List of BenchmarkRun objects, oldest first.
        """
        if not path.exists():
            return []

        runs: list[BenchmarkRun] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(BenchmarkRun.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
        return runs

    # ------------------------------------------------------------------
    # Convenience: run + compare in one call
    # ------------------------------------------------------------------

    def run_and_compare(self) -> tuple[BenchmarkRun, RegressionReport | None]:
        """Execute a benchmark run and optionally compare to a saved baseline.

        If :attr:`BenchmarkConfig.baseline_path` is set and the file contains
        at least one prior run, comparison is performed automatically.

        Returns:
            Tuple of (current_run, regression_report_or_None).
        """
        current = self.run()

        report: RegressionReport | None = None
        if self._config.baseline_path:
            prior_runs = self.load(self._config.baseline_path)
            if prior_runs:
                baseline = prior_runs[-1]  # most recent prior run
                report = self.compare_to_baseline(current, baseline)

        if self._config.output_dir:
            self.save(current, self._config.output_dir)

        return current, report
