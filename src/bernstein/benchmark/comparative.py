"""Comparative Benchmark Suite for Bernstein.

Runs the same set of benchmark tasks in both single-agent and orchestrated
(multi-agent) modes, then produces a side-by-side comparison report with
wall-time, cost, token-usage, and success-rate metrics.

Usage::

    tasks = load_benchmark_tasks(Path("templates/benchmarks"))
    suite = ComparativeBenchmark(tasks=tasks, workdir=Path("."))
    report = suite.run_suite(modes=["single", "orchestrated"])
    print(suite.generate_markdown_report(report))
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import yaml

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.adapters.base import CLIAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

TaskType = Literal["bugfix", "test", "refactor", "docs"]
Mode = Literal["single", "orchestrated"]


@dataclass(frozen=True)
class BenchmarkTask:
    """A single benchmark task specification.

    Args:
        task_id: Unique identifier for the task.
        description: Human-readable description of what needs to be done.
        task_type: Category of work (bugfix, test, refactor, docs).
        files: List of file paths relevant to the task.
        expected_outcome: Description of what a successful completion looks like.
    """

    task_id: str
    description: str
    task_type: TaskType
    files: list[str]
    expected_outcome: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BenchmarkTask:
        """Parse a BenchmarkTask from a raw dictionary.

        Args:
            raw: Dict with task fields (typically loaded from YAML).

        Returns:
            Parsed BenchmarkTask.

        Raises:
            KeyError: If required fields are missing.
        """
        return cls(
            task_id=str(raw["task_id"]),
            description=str(raw["description"]),
            task_type=raw["task_type"],
            files=list(raw.get("files", [])),
            expected_outcome=str(raw["expected_outcome"]),
        )


@dataclass
class BenchmarkResult:
    """Result of running a single benchmark task in a specific mode.

    Args:
        task_id: ID of the benchmark task that was run.
        mode: Execution mode used (single-agent or orchestrated).
        wall_time_seconds: Wall-clock duration of the run.
        cost_usd: Estimated LLM API cost in USD.
        tokens_used: Total tokens consumed (prompt + completion).
        success: Whether the task completed without errors.
        verification_passed: Whether the output passed verification checks.
    """

    task_id: str
    mode: Mode
    wall_time_seconds: float
    cost_usd: float
    tokens_used: int
    success: bool
    verification_passed: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON/YAML export.

        Returns:
            Dict with all fields.
        """
        return {
            "task_id": self.task_id,
            "mode": self.mode,
            "wall_time_seconds": round(self.wall_time_seconds, 2),
            "cost_usd": round(self.cost_usd, 4),
            "tokens_used": self.tokens_used,
            "success": self.success,
            "verification_passed": self.verification_passed,
        }


@dataclass
class ModeSummary:
    """Aggregated metrics for a single execution mode.

    Args:
        mode: The execution mode these metrics describe.
        total_tasks: Number of tasks attempted.
        successes: Number of tasks that succeeded.
        success_rate: Fraction of tasks that succeeded (0.0-1.0).
        avg_wall_time: Mean wall-clock time across tasks.
        median_wall_time: Median wall-clock time across tasks.
        total_cost_usd: Sum of all task costs.
        avg_cost_usd: Mean cost per task.
        total_tokens: Sum of all tokens consumed.
        verification_rate: Fraction of tasks that passed verification.
    """

    mode: Mode
    total_tasks: int
    successes: int
    success_rate: float
    avg_wall_time: float
    median_wall_time: float
    total_cost_usd: float
    avg_cost_usd: float
    total_tokens: int
    verification_rate: float


@dataclass
class BenchmarkReport:
    """Aggregate report comparing benchmark results across modes.

    Args:
        results: All individual benchmark results.
        summary: Per-mode aggregated metrics.
    """

    results: list[BenchmarkResult] = field(default_factory=list)
    summary: dict[str, ModeSummary] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Summary computation
# ---------------------------------------------------------------------------


def _compute_mode_summary(mode: Mode, results: list[BenchmarkResult]) -> ModeSummary:
    """Compute aggregated metrics for a single mode.

    Args:
        mode: The execution mode.
        results: Results filtered to this mode.

    Returns:
        ModeSummary with aggregated statistics.
    """
    if not results:
        return ModeSummary(
            mode=mode,
            total_tasks=0,
            successes=0,
            success_rate=0.0,
            avg_wall_time=0.0,
            median_wall_time=0.0,
            total_cost_usd=0.0,
            avg_cost_usd=0.0,
            total_tokens=0,
            verification_rate=0.0,
        )

    successes = sum(1 for r in results if r.success)
    verified = sum(1 for r in results if r.verification_passed)
    times = [r.wall_time_seconds for r in results]
    total_cost = sum(r.cost_usd for r in results)
    total_tokens = sum(r.tokens_used for r in results)
    n = len(results)

    return ModeSummary(
        mode=mode,
        total_tasks=n,
        successes=successes,
        success_rate=successes / n,
        avg_wall_time=statistics.mean(times),
        median_wall_time=statistics.median(times),
        total_cost_usd=total_cost,
        avg_cost_usd=total_cost / n,
        total_tokens=total_tokens,
        verification_rate=verified / n,
    )


def compute_report(results: list[BenchmarkResult]) -> BenchmarkReport:
    """Build an aggregate report from a flat list of results.

    Args:
        results: All individual benchmark results across all modes.

    Returns:
        BenchmarkReport with per-mode summaries.
    """
    by_mode: dict[str, list[BenchmarkResult]] = {}
    for r in results:
        by_mode.setdefault(r.mode, []).append(r)

    summary: dict[str, ModeSummary] = {}
    for mode_key, mode_results in by_mode.items():
        summary[mode_key] = _compute_mode_summary(mode_key, mode_results)  # type: ignore[arg-type]

    return BenchmarkReport(results=list(results), summary=summary)


# ---------------------------------------------------------------------------
# YAML task loader
# ---------------------------------------------------------------------------


def load_benchmark_tasks(tasks_dir: Path) -> list[BenchmarkTask]:
    """Load benchmark task definitions from YAML files in a directory.

    Each ``.yaml`` / ``.yml`` file in ``tasks_dir`` should contain a single
    task definition with fields matching :class:`BenchmarkTask`.

    Args:
        tasks_dir: Directory containing YAML task specification files.

    Returns:
        List of parsed BenchmarkTask objects, sorted by task_id.
    """
    tasks: list[BenchmarkTask] = []
    if not tasks_dir.is_dir():
        logger.warning("Benchmark tasks directory does not exist: %s", tasks_dir)
        return tasks

    for path in sorted(tasks_dir.iterdir()):
        if path.suffix not in (".yaml", ".yml"):
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if raw is None:
                continue
            tasks.append(BenchmarkTask.from_dict(raw))
        except (yaml.YAMLError, KeyError, TypeError) as exc:
            logger.warning("Skipping invalid benchmark file %s: %s", path.name, exc)
            continue

    return sorted(tasks, key=lambda t: t.task_id)


# ---------------------------------------------------------------------------
# Comparative Benchmark Suite
# ---------------------------------------------------------------------------


class ComparativeBenchmark:
    """Runs benchmark tasks in single-agent and orchestrated modes and compares.

    Args:
        tasks: List of benchmark tasks to execute.
        workdir: Working directory for agent execution.
    """

    def __init__(self, tasks: list[BenchmarkTask], workdir: Path) -> None:
        self._tasks = tasks
        self._workdir = workdir

    @property
    def tasks(self) -> list[BenchmarkTask]:
        """The benchmark tasks this suite will execute."""
        return list(self._tasks)

    # ------------------------------------------------------------------
    # Single-agent execution
    # ------------------------------------------------------------------

    def run_single_agent(
        self,
        task: BenchmarkTask,
        adapter: CLIAdapter,
    ) -> BenchmarkResult:
        """Run a benchmark task using a single CLI agent.

        Args:
            task: The benchmark task to execute.
            adapter: CLI adapter instance for spawning the agent.

        Returns:
            BenchmarkResult with timing, cost, and success metrics.
        """
        from bernstein.core.models import ModelConfig

        t0 = time.monotonic()
        prompt = f"Task: {task.description}\nFiles: {', '.join(task.files)}\nExpected: {task.expected_outcome}"

        success = False
        cost_usd = 0.0
        tokens_used = 0
        try:
            model_config = ModelConfig(model="default", provider="default")
            adapter.spawn(
                prompt=prompt,
                workdir=self._workdir,
                model_config=model_config,
                role="benchmark",
                session_id=f"bench-{task.task_id}",
                pid_dir=self._workdir / ".sdd" / "runtime" / "pids",
            )
            success = True
        except Exception:
            logger.exception("Single-agent run failed for task %s", task.task_id)

        wall_time = time.monotonic() - t0

        return BenchmarkResult(
            task_id=task.task_id,
            mode="single",
            wall_time_seconds=wall_time,
            cost_usd=cost_usd,
            tokens_used=tokens_used,
            success=success,
            verification_passed=success,
        )

    # ------------------------------------------------------------------
    # Orchestrated execution
    # ------------------------------------------------------------------

    def run_orchestrated(
        self,
        task: BenchmarkTask,
        config: dict[str, Any] | None = None,
    ) -> BenchmarkResult:
        """Run a benchmark task through the Bernstein orchestration pipeline.

        Args:
            task: The benchmark task to execute.
            config: Optional orchestrator configuration overrides.

        Returns:
            BenchmarkResult with timing, cost, and success metrics.
        """
        import subprocess

        t0 = time.monotonic()
        success = False
        cost_usd = 0.0
        tokens_used = 0

        goal = f"{task.description}\nFiles: {', '.join(task.files)}\nExpected: {task.expected_outcome}"

        try:
            budget = "2.00"
            if config:
                budget = str(config.get("budget", "2.00"))

            proc = subprocess.run(
                ["bernstein", "--goal", goal, "--headless", "--budget", budget],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                timeout=600,
            )
            success = proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            logger.exception("Orchestrated run failed for task %s", task.task_id)

        wall_time = time.monotonic() - t0

        return BenchmarkResult(
            task_id=task.task_id,
            mode="orchestrated",
            wall_time_seconds=wall_time,
            cost_usd=cost_usd,
            tokens_used=tokens_used,
            success=success,
            verification_passed=success,
        )

    # ------------------------------------------------------------------
    # Full suite execution
    # ------------------------------------------------------------------

    def run_suite(
        self,
        modes: list[Mode] | None = None,
    ) -> BenchmarkReport:
        """Run all benchmark tasks in the specified modes and produce a report.

        Args:
            modes: Execution modes to run. Defaults to both
                ``["single", "orchestrated"]``.

        Returns:
            BenchmarkReport with results and per-mode summaries.
        """
        if modes is None:
            modes = ["single", "orchestrated"]

        all_results: list[BenchmarkResult] = []

        for task in self._tasks:
            for mode in modes:
                logger.info("Running task %s in %s mode", task.task_id, mode)
                result = self._run_single_stub(task) if mode == "single" else self._run_orchestrated_stub(task)
                all_results.append(result)

        return compute_report(all_results)

    def _run_single_stub(self, task: BenchmarkTask) -> BenchmarkResult:
        """Stub for single-agent execution within run_suite.

        Override or replace with ``run_single_agent`` when an adapter is available.
        """
        return BenchmarkResult(
            task_id=task.task_id,
            mode="single",
            wall_time_seconds=0.0,
            cost_usd=0.0,
            tokens_used=0,
            success=False,
            verification_passed=False,
        )

    def _run_orchestrated_stub(self, task: BenchmarkTask) -> BenchmarkResult:
        """Stub for orchestrated execution within run_suite.

        Override or replace with ``run_orchestrated`` when Bernstein is available.
        """
        return BenchmarkResult(
            task_id=task.task_id,
            mode="orchestrated",
            wall_time_seconds=0.0,
            cost_usd=0.0,
            tokens_used=0,
            success=False,
            verification_passed=False,
        )

    # ------------------------------------------------------------------
    # Markdown report
    # ------------------------------------------------------------------

    def generate_markdown_report(self, report: BenchmarkReport) -> str:
        """Generate a markdown comparison table from a benchmark report.

        Args:
            report: The completed benchmark report.

        Returns:
            Markdown string with per-task and summary comparison tables.
        """
        lines: list[str] = []
        lines.append("# Comparative Benchmark Report")
        lines.append("")

        # Per-task results table
        lines.append("## Per-Task Results")
        lines.append("")
        lines.append("| Task ID | Mode | Wall Time (s) | Cost (USD) | Tokens | Success | Verified |")
        lines.append("|---------|------|---------------|------------|--------|---------|----------|")

        for r in sorted(report.results, key=lambda x: (x.task_id, x.mode)):
            success_mark = "Yes" if r.success else "No"
            verified_mark = "Yes" if r.verification_passed else "No"
            lines.append(
                f"| {r.task_id} | {r.mode} | {r.wall_time_seconds:.2f} "
                f"| ${r.cost_usd:.4f} | {r.tokens_used} "
                f"| {success_mark} | {verified_mark} |"
            )

        lines.append("")

        # Summary comparison table
        if report.summary:
            lines.append("## Mode Comparison Summary")
            lines.append("")
            lines.append("| Metric | " + " | ".join(report.summary.keys()) + " |")
            lines.append("|--------|" + "|".join("--------" for _ in report.summary) + "|")

            summaries = list(report.summary.values())
            metrics: list[tuple[str, Any]] = [
                ("Total Tasks", [s.total_tasks for s in summaries]),
                ("Successes", [s.successes for s in summaries]),
                ("Success Rate", [f"{s.success_rate:.1%}" for s in summaries]),
                ("Avg Wall Time (s)", [f"{s.avg_wall_time:.2f}" for s in summaries]),
                ("Median Wall Time (s)", [f"{s.median_wall_time:.2f}" for s in summaries]),
                ("Total Cost (USD)", [f"${s.total_cost_usd:.4f}" for s in summaries]),
                ("Avg Cost (USD)", [f"${s.avg_cost_usd:.4f}" for s in summaries]),
                ("Total Tokens", [s.total_tokens for s in summaries]),
                ("Verification Rate", [f"{s.verification_rate:.1%}" for s in summaries]),
            ]

            for metric_name, values in metrics:
                cells = " | ".join(str(v) for v in values)
                lines.append(f"| {metric_name} | {cells} |")

            lines.append("")

        return "\n".join(lines)
