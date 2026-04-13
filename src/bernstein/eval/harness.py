"""Eval harness — multiplicative scoring, LLM judge, failure taxonomy.

Provides tiered evaluation with the scoring formula:
    Score = (0.5*TaskSuccess + 0.3*CodeQuality + 0.2*Efficiency) * Reliability * Safety

Multiplicative gates (Reliability, Safety) mean one test regression = zero score.

Tiers:
- smoke: ~5 tasks, fast, used by the evolution eval gate (~$0.50)
- standard: ~15 tasks, moderate, used for logic changes (~$2.00)
- full: all tasks, comprehensive, used for manual validation
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from bernstein.eval.golden import GoldenTask, Tier, load_golden_tasks
from bernstein.eval.metrics import (
    EvalScoreComponents,
    TierScores,
    compute_efficiency,
    compute_reliability,
    compute_safety,
)
from bernstein.eval.taxonomy import (
    FailureRecord,
    FailureTaxonomy,
    classify_failure,
)
from bernstein.eval.telemetry import AgentTelemetry, parse_telemetry
from bernstein.evolution.benchmark import (
    RunSummary,
    load_benchmarks,
    run_benchmark,
    save_results,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.eval.judge import JudgeVerdict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Eval tiers
# ---------------------------------------------------------------------------


class EvalTier(Enum):
    """Evaluation tiers with increasing coverage and cost."""

    SMOKE = "smoke"  # ~5 tasks, ~$0.50
    STANDARD = "standard"  # ~15 tasks, ~$2.00
    FULL = "full"  # All tasks


# Map eval tiers to benchmark tiers to run.
_TIER_TO_BENCHMARK_TIERS: dict[EvalTier, tuple[str, ...]] = {
    EvalTier.SMOKE: ("smoke",),
    EvalTier.STANDARD: ("smoke", "capability"),
    EvalTier.FULL: ("smoke", "capability", "stretch"),
}

# Map eval tiers to golden tiers for multiplicative scoring.
_TIER_TO_GOLDEN_TIERS: dict[EvalTier, tuple[Tier, ...]] = {
    EvalTier.SMOKE: ("smoke",),
    EvalTier.STANDARD: ("smoke", "standard"),
    EvalTier.FULL: ("smoke", "standard", "stretch", "adversarial"),
}


def _run_benchmark_tier(
    benchmarks_dir: Path,
    tier: str,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Run all benchmarks for a single tier.

    Returns:
        Tuple of (passed_count, total_count, detail_dicts).
        When no specs are found, returns ``(0, 0, [])``.
    """
    specs = load_benchmarks(benchmarks_dir, tier)  # type: ignore[arg-type]
    if not specs:
        return 0, 0, []

    tier_passed = 0
    details: list[dict[str, Any]] = []
    for spec in specs:
        result = run_benchmark(spec)
        if result.passed:
            tier_passed += 1
        details.append(
            {
                "benchmark_id": result.benchmark_id,
                "tier": result.tier,
                "passed": result.passed,
                "duration_seconds": result.duration_seconds,
            }
        )
    return tier_passed, len(specs), details


# ---------------------------------------------------------------------------
# Per-task eval result
# ---------------------------------------------------------------------------


@dataclass
class TaskEvalResult:
    """Result of evaluating a single golden task.

    Attributes:
        task_id: ID of the evaluated golden task.
        tier: Task difficulty tier.
        passed: Whether all completion signals passed.
        telemetry: Agent telemetry from the run.
        judge_verdict: LLM judge verdict (if judge was run).
        failure: Classified failure record (if task failed).
        duration_s: Wall-clock seconds for this task.
        cost_usd: Estimated cost for this task.
    """

    task_id: str
    tier: Tier
    passed: bool = False
    telemetry: AgentTelemetry | None = None
    judge_verdict: JudgeVerdict | None = None
    failure: FailureRecord | None = None
    duration_s: float = 0.0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Run-level result (backward-compatible with existing EvalResult interface)
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    """Result of an evaluation run.

    Supports both the legacy simple score and the new multiplicative scoring.

    Attributes:
        score: Composite score (0.0 - 1.0).
        components: Per-tier or per-dimension scores.
        tier: Which eval tier was run.
        tasks_evaluated: Number of benchmark tasks evaluated.
        duration_seconds: Wall-clock time for the eval run.
        timestamp: Unix timestamp of the result.
        details: Raw benchmark results for debugging.
        multiplicative_components: Full multiplicative score breakdown.
        per_tier: Per-tier scores from golden suite.
        failures: Failure taxonomy records.
        task_results: Per-task eval results.
        cost_total: Total cost across all tasks.
    """

    score: float
    components: dict[str, float] = field(default_factory=dict[str, float])
    tier: str = "smoke"
    tasks_evaluated: int = 0
    duration_seconds: float = 0.0
    timestamp: float = field(default_factory=time.time)
    details: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    # New multiplicative scoring fields
    multiplicative_components: EvalScoreComponents | None = None
    per_tier: TierScores | None = None
    failures: list[FailureRecord] = field(default_factory=list[FailureRecord])
    task_results: list[TaskEvalResult] = field(default_factory=list[TaskEvalResult])
    cost_total: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON output."""
        result: dict[str, Any] = {
            "score": round(self.score, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "tier": self.tier,
            "tasks_evaluated": self.tasks_evaluated,
            "duration_seconds": round(self.duration_seconds, 2),
            "timestamp": self.timestamp,
        }

        if self.multiplicative_components is not None:
            mc = self.multiplicative_components
            result["multiplicative_components"] = {
                "task_success": round(mc.task_success, 4),
                "code_quality": round(mc.code_quality, 4),
                "efficiency": round(mc.efficiency, 4),
                "reliability": round(mc.reliability, 4),
                "safety": round(mc.safety, 4),
            }

        if self.per_tier is not None:
            result["per_tier"] = {
                "smoke": round(self.per_tier.smoke, 4),
                "standard": round(self.per_tier.standard, 4),
                "stretch": round(self.per_tier.stretch, 4),
                "adversarial": round(self.per_tier.adversarial, 4),
            }

        if self.failures:
            result["failures"] = [
                {
                    "task": f.task_id,
                    "taxonomy": f.category.value,
                    "details": f.details,
                }
                for f in self.failures
            ]

        if self.cost_total > 0:
            result["cost_total"] = round(self.cost_total, 4)

        return result


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class EvalHarness:
    """Evaluation harness with multiplicative scoring.

    Runs benchmarks at the requested tier and computes a composite score.
    Supports both the legacy benchmark-based scoring and the new
    golden-task multiplicative scoring with LLM judge.

    Args:
        state_dir: Path to .sdd directory.
        repo_root: Repository root (defaults to state_dir.parent).
        golden_dir: Directory containing golden benchmark tasks.
        runs_dir: Directory to write eval run results.
    """

    def __init__(
        self,
        state_dir: Path,
        repo_root: Path | None = None,
        golden_dir: Path | None = None,
        runs_dir: Path | None = None,
    ) -> None:
        self._state_dir = state_dir
        self._repo_root = repo_root or state_dir.parent
        self._benchmarks_dir = self._repo_root / "tests" / "benchmarks"
        self._golden_dir = golden_dir or (state_dir / "eval" / "golden")
        self._runs_dir = runs_dir or (state_dir / "eval" / "runs")
        self._taxonomy = FailureTaxonomy()

    # ------------------------------------------------------------------
    # Legacy benchmark-based scoring (backward compatible)
    # ------------------------------------------------------------------

    def run(
        self,
        tier: str = "smoke",
        sandbox_dir: Path | None = None,
    ) -> EvalResult:
        """Run evaluation at the specified tier.

        Uses legacy benchmark runner for backward compatibility.

        Args:
            tier: Evaluation tier ("smoke", "standard", or "full").
            sandbox_dir: If provided, run benchmarks against this directory.

        Returns:
            EvalResult with composite score and per-tier breakdowns.
        """
        start = time.time()
        eval_tier = EvalTier(tier)
        benchmark_tiers = _TIER_TO_BENCHMARK_TIERS[eval_tier]

        benchmarks_dir = (sandbox_dir / "tests" / "benchmarks") if sandbox_dir else self._benchmarks_dir

        if not benchmarks_dir.is_dir():
            logger.debug("No benchmarks directory at %s — returning score 1.0", benchmarks_dir)
            return EvalResult(
                score=1.0,
                components={},
                tier=tier,
                tasks_evaluated=0,
                duration_seconds=time.time() - start,
            )

        components: dict[str, float] = {}
        all_details: list[dict[str, Any]] = []
        total_passed = 0
        total_tasks = 0

        for bt in benchmark_tiers:
            tier_passed, tier_total, tier_details = _run_benchmark_tier(benchmarks_dir, bt)
            if tier_total == 0:
                continue
            all_details.extend(tier_details)
            total_passed += tier_passed
            total_tasks += tier_total
            components[bt] = tier_passed / tier_total

        score = total_passed / total_tasks if total_tasks > 0 else 1.0
        duration = time.time() - start

        if total_tasks > 0:
            summary = RunSummary(
                tier=tier,
                total=total_tasks,
                passed=total_passed,
                failed=total_tasks - total_passed,
            )
            try:
                save_results(summary, self._state_dir)
            except OSError:
                logger.warning("Failed to save eval results")

        logger.info(
            "Eval harness [%s]: %d/%d passed (score=%.4f) in %.1fs",
            tier,
            total_passed,
            total_tasks,
            score,
            duration,
        )

        return EvalResult(
            score=score,
            components=components,
            tier=tier,
            tasks_evaluated=total_tasks,
            duration_seconds=round(duration, 2),
            details=all_details,
        )

    # ------------------------------------------------------------------
    # Golden task loading
    # ------------------------------------------------------------------

    def load_golden_tasks(self, tier_filter: Tier | None = None) -> list[GoldenTask]:
        """Load golden tasks, optionally filtered by tier."""
        return load_golden_tasks(self._golden_dir, tier_filter=tier_filter)

    # ------------------------------------------------------------------
    # Multiplicative scoring
    # ------------------------------------------------------------------

    def evaluate_task(
        self,
        task: GoldenTask,
        telemetry_raw: dict[str, object] | None = None,
        judge_verdict: JudgeVerdict | None = None,
    ) -> TaskEvalResult:
        """Evaluate a single task against its golden expectations.

        Synchronous scoring path — takes pre-collected telemetry and
        an optional judge verdict, produces a TaskEvalResult.

        Args:
            task: The golden task to evaluate.
            telemetry_raw: Raw telemetry dict from agent output.
            judge_verdict: Pre-computed judge verdict.

        Returns:
            TaskEvalResult with pass/fail and classified failure.
        """
        result = TaskEvalResult(task_id=task.id, tier=task.tier)
        start = time.monotonic()

        # Parse and validate telemetry
        if telemetry_raw:
            telemetry = parse_telemetry(telemetry_raw)
            result.telemetry = telemetry
            result.cost_usd = telemetry.cost_usd
        else:
            telemetry = AgentTelemetry(task_id=task.id)
            result.telemetry = telemetry

        result.judge_verdict = judge_verdict

        # Determine pass/fail from completion signals in telemetry
        signals_ok = (
            telemetry.completion_signals_checked > 0
            and telemetry.completion_signals_passed == telemetry.completion_signals_checked
        )
        tests_ok = telemetry.tests_failed == 0
        result.passed = signals_ok and tests_ok

        result.duration_s = time.monotonic() - start

        # Classify failure if not passed
        if not result.passed:
            timed_out = telemetry.duration_s > task.max_duration_s if task.max_duration_s > 0 else False
            tests_regressed = telemetry.tests_failed > 0

            scope_violated = False
            if task.owned_files and telemetry.files_modified:
                scope_violated = any(f not in task.owned_files for f in telemetry.files_modified)

            total_file_ops = len(telemetry.files_read) + len(telemetry.files_modified)
            orientation_ratio = len(telemetry.files_read) / total_file_ops if total_file_ops > 0 else 0.0

            failure = classify_failure(
                task_id=task.id,
                timed_out=timed_out,
                tests_regressed=tests_regressed,
                scope_violated=scope_violated,
                signals_incomplete=not signals_ok,
                orientation_ratio=orientation_ratio,
                files_involved=telemetry.files_modified,
            )
            result.failure = failure
            self._taxonomy.add(failure)

        return result

    def compute_multiplicative_score(
        self,
        task_results: list[TaskEvalResult],
        crash_count: int = 0,
        orphan_count: int = 0,
    ) -> EvalResult:
        """Compute the multiplicative score from evaluated task results.

        Score = (0.5*TaskSuccess + 0.3*CodeQuality + 0.2*Efficiency) * Reliability * Safety

        Args:
            task_results: Results from all evaluated tasks.
            crash_count: Number of agent crashes during the run.
            orphan_count: Number of orphaned agent processes.

        Returns:
            EvalResult with full multiplicative score breakdown.
        """
        start = time.time()

        if not task_results:
            return EvalResult(score=0.0, tier="full", tasks_evaluated=0)

        # Task success rate
        total = len(task_results)
        passed = sum(1 for r in task_results if r.passed)
        task_success = passed / total if total > 0 else 0.0

        # Code quality from judge verdicts
        verdicts = [r.judge_verdict for r in task_results if r.judge_verdict is not None]
        code_quality = sum(v.average_score for v in verdicts) / len(verdicts) if verdicts else 0.0

        # Efficiency from telemetry
        telemetry_list = [r.telemetry for r in task_results if r.telemetry is not None]
        efficiency = compute_efficiency(telemetry_list, passed)

        # Telemetry validity
        all_telemetry_valid = all(r.telemetry is not None for r in task_results)

        # Reliability gate
        reliability = compute_reliability(
            crash_count=crash_count,
            orphan_count=orphan_count,
            telemetry_valid=all_telemetry_valid,
        )

        # Safety gate
        has_regressions = self._taxonomy.has_test_regressions()
        safety = compute_safety(has_regressions)

        # Assemble components
        mc = EvalScoreComponents(
            task_success=task_success,
            code_quality=code_quality,
            efficiency=efficiency,
            reliability=reliability,
            safety=safety,
        )

        # Per-tier scores
        tier_buckets: dict[str, list[TaskEvalResult]] = {}
        for r in task_results:
            tier_buckets.setdefault(r.tier, []).append(r)

        def _tier_rate(results: list[TaskEvalResult]) -> float:
            if not results:
                return 0.0
            return sum(1 for r in results if r.passed) / len(results)

        per_tier = TierScores(
            smoke=_tier_rate(tier_buckets.get("smoke", [])),
            standard=_tier_rate(tier_buckets.get("standard", [])),
            stretch=_tier_rate(tier_buckets.get("stretch", [])),
            adversarial=_tier_rate(tier_buckets.get("adversarial", [])),
        )

        duration = time.time() - start

        return EvalResult(
            score=mc.final_score,
            components={
                "task_success": task_success,
                "code_quality": code_quality,
                "efficiency": efficiency,
                "reliability": reliability,
                "safety": safety,
            },
            tier="full",
            tasks_evaluated=total,
            duration_seconds=round(duration, 2),
            multiplicative_components=mc,
            per_tier=per_tier,
            failures=list(self._taxonomy.failures),
            task_results=task_results,
            cost_total=sum(r.cost_usd for r in task_results),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_run(self, result: EvalResult) -> Path:
        """Save a run result to disk as JSON.

        Args:
            result: The completed eval run result.

        Returns:
            Path to the saved JSON file.
        """
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        path = self._runs_dir / f"eval_run_{ts}.json"
        path.write_text(
            json.dumps(result.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("Eval run saved to %s", path)
        return path

    def load_previous_run(self) -> EvalResult | None:
        """Load the most recent previous run for comparison.

        Returns:
            The most recent EvalResult, or None if no runs exist.
        """
        if not self._runs_dir.is_dir():
            return None

        run_files = sorted(self._runs_dir.glob("eval_run_*.json"), reverse=True)
        if not run_files:
            return None

        try:
            data = json.loads(run_files[0].read_text(encoding="utf-8"))
            mc_data = data.get("multiplicative_components")
            mc = None
            if mc_data:
                mc = EvalScoreComponents(
                    task_success=mc_data.get("task_success", 0.0),
                    code_quality=mc_data.get("code_quality", 0.0),
                    efficiency=mc_data.get("efficiency", 0.0),
                    reliability=mc_data.get("reliability", 1.0),
                    safety=mc_data.get("safety", 1.0),
                )

            pt_data = data.get("per_tier")
            pt = None
            if pt_data:
                pt = TierScores(
                    smoke=pt_data.get("smoke", 0.0),
                    standard=pt_data.get("standard", 0.0),
                    stretch=pt_data.get("stretch", 0.0),
                    adversarial=pt_data.get("adversarial", 0.0),
                )

            return EvalResult(
                score=data.get("score", 0.0),
                components=data.get("components", {}),
                tier=data.get("tier", "smoke"),
                tasks_evaluated=data.get("tasks_evaluated", 0),
                duration_seconds=data.get("duration_seconds", 0.0),
                timestamp=data.get("timestamp", 0.0),
                multiplicative_components=mc,
                per_tier=pt,
                cost_total=data.get("cost_total", 0.0),
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load previous run: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Failure taxonomy access
    # ------------------------------------------------------------------

    @property
    def taxonomy(self) -> FailureTaxonomy:
        """Access the failure taxonomy for the current run."""
        return self._taxonomy
