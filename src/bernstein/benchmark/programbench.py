"""ProgramBench evaluation harness for Bernstein.

Runs Bernstein against ProgramBench tasks and reports partial-credit
scoring metrics alongside the existing SWE-Bench harness. ProgramBench
tasks include a state setup, a target implementation, and a set of
runtime asserts; the per-task score is the fraction of asserts that pass.

Usage::

    harness = ProgramBenchHarness(workdir=Path("."), sample=20)
    tasks = harness.load_dataset()
    report = harness.run(adapter="claude", tasks=tasks)
    save_results(report, Path(".sdd"))
"""

from __future__ import annotations

import json
import random
import statistics
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


type _CAST_LIST_ANY = list[Any]
_NEAR_SOLVED_THRESHOLD = 0.5

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgramBenchTask:
    """A single ProgramBench evaluation task.

    Args:
        task_id: Unique identifier, e.g. ``programbench-001``.
        subset: Subset slug the task belongs to (e.g. ``lite``).
        description: Natural-language description of the feature to build.
        setup_code: Python code that establishes the initial state.
        target_signature: Function signature the agent must implement.
        asserts: Runtime assertion expressions evaluated against the agent's
            output. Each entry is a Python expression returning a bool.
        hidden_asserts: Extra asserts not shown to the agent.
        tags: Free-form classification labels.
    """

    task_id: str
    subset: str
    description: str
    setup_code: str
    target_signature: str
    asserts: list[str]
    hidden_asserts: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProgramBenchTask:
        """Parse a ProgramBenchTask from the raw dataset format.

        Args:
            raw: Dict with ProgramBench dataset fields.

        Returns:
            Parsed ProgramBenchTask.

        Raises:
            KeyError: If a required field is missing.
        """

        def _parse_str_list(value: Any) -> list[str]:
            if isinstance(value, list):
                lst = cast(_CAST_LIST_ANY, value)
                return [str(v) for v in lst]
            if isinstance(value, str):
                with suppress(json.JSONDecodeError):
                    parsed: Any = json.loads(value)
                    if isinstance(parsed, list):
                        plst = cast(_CAST_LIST_ANY, parsed)
                        return [str(v) for v in plst]
                return [value] if value else []
            return []

        if "task_id" not in raw and "id" not in raw:
            raise KeyError("ProgramBench task missing 'task_id'")
        task_id = str(raw.get("task_id", raw.get("id", "")))
        if not task_id:
            raise KeyError("ProgramBench task has empty 'task_id'")

        return cls(
            task_id=task_id,
            subset=str(raw.get("subset", "lite")),
            description=str(raw.get("description", raw.get("problem_statement", ""))),
            setup_code=str(raw.get("setup_code", raw.get("setup", ""))),
            target_signature=str(raw.get("target_signature", raw.get("signature", ""))),
            asserts=_parse_str_list(raw.get("asserts", raw.get("ASSERTS", []))),
            hidden_asserts=_parse_str_list(raw.get("hidden_asserts", [])),
            tags=_parse_str_list(raw.get("tags", [])),
        )


@dataclass
class TaskResult:
    """Result of running Bernstein on a single ProgramBench task.

    Args:
        task_id: Matches the ProgramBenchTask this result is for.
        score: Partial-credit score in ``[0, 1]``: ``asserts_passed / asserts_total``.
        asserts_passed: Number of asserts that returned True.
        asserts_total: Total number of asserts evaluated.
        fully_solved: True iff every assert passed (score == 1.0).
        cost_usd: Estimated LLM API cost in USD attributable to this task.
        duration_seconds: Wall-clock time taken.
        adapter: Adapter that produced the candidate.
        log_path: Optional path to the per-task log file.
        error: Error message if the run failed, else None.
    """

    task_id: str
    score: float
    asserts_passed: int
    asserts_total: int
    fully_solved: bool
    cost_usd: float
    duration_seconds: float
    adapter: str
    log_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON export.

        Returns:
            Dict with all fields.
        """
        return {
            "task_id": self.task_id,
            "score": self.score,
            "asserts_passed": self.asserts_passed,
            "asserts_total": self.asserts_total,
            "fully_solved": self.fully_solved,
            "cost_usd": self.cost_usd,
            "duration_seconds": self.duration_seconds,
            "adapter": self.adapter,
            "log_path": self.log_path,
            "error": self.error,
        }


@dataclass
class AdapterBreakdown:
    """Aggregate ProgramBench metrics for a single adapter.

    Args:
        adapter: Adapter name.
        total: Total tasks evaluated with this adapter.
        fully_solved: Number of tasks with score == 1.0.
        near_solved: Number of tasks with score >= 0.5 but < 1.0.
        failed: Number of tasks with score < 0.5.
        mean_partial_credit: Mean per-task score.
        cost_per_task: Mean cost per task.
        time_per_task: Mean duration per task.
    """

    adapter: str
    total: int
    fully_solved: int
    near_solved: int
    failed: int
    mean_partial_credit: float
    cost_per_task: float
    time_per_task: float

    def to_dict(self) -> dict[str, float | int | str]:
        """Serialise the breakdown for JSON output.

        Returns:
            Plain JSON-compatible mapping.
        """
        return {
            "adapter": self.adapter,
            "total": self.total,
            "fully_solved": self.fully_solved,
            "near_solved": self.near_solved,
            "failed": self.failed,
            "mean_partial_credit": self.mean_partial_credit,
            "cost_per_task": self.cost_per_task,
            "time_per_task": self.time_per_task,
        }


@dataclass
class ProgramBenchReport:
    """Aggregate report for a ProgramBench evaluation run.

    Args:
        total_tasks: Total number of tasks evaluated.
        fully_solved: Number of tasks with score == 1.0.
        near_solved: Number of tasks with 0.5 <= score < 1.0.
        failed: Number of tasks with score < 0.5.
        mean_partial_credit: Mean partial-credit score across all tasks.
        total_cost_usd: Sum of per-task costs.
        time_per_task: Mean wall-clock time per task.
        median_cost_usd: Median cost across all tasks.
        median_duration_seconds: Median duration across all tasks.
        per_adapter_breakdown: Per-adapter aggregate metrics.
        per_task: Per-task results.
        run_at: ISO-8601 timestamp when the report was generated.
    """

    total_tasks: int
    fully_solved: int
    near_solved: int
    failed: int
    mean_partial_credit: float
    total_cost_usd: float
    time_per_task: float
    median_cost_usd: float
    median_duration_seconds: float
    per_adapter_breakdown: list[AdapterBreakdown]
    per_task: list[TaskResult]
    run_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def compute_score(asserts_passed: int, asserts_total: int) -> float:
    """Compute partial-credit score clamped to ``[0, 1]``.

    Args:
        asserts_passed: Number of asserts that passed.
        asserts_total: Total number of asserts.

    Returns:
        Fraction in ``[0, 1]``. Returns ``0.0`` if ``asserts_total <= 0``.
    """
    if asserts_total <= 0:
        return 0.0
    if asserts_passed <= 0:
        return 0.0
    raw = asserts_passed / asserts_total
    if raw > 1.0:
        return 1.0
    return raw


def classify_score(score: float) -> str:
    """Classify a score into ``fully_solved`` / ``near_solved`` / ``failed``.

    Args:
        score: Per-task score in ``[0, 1]``.

    Returns:
        Bucket name.
    """
    if score >= 1.0:
        return "fully_solved"
    if score >= _NEAR_SOLVED_THRESHOLD:
        return "near_solved"
    return "failed"


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return statistics.median(values)


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return statistics.fmean(values)


def _compute_adapter_breakdown(results: list[TaskResult]) -> list[AdapterBreakdown]:
    grouped: dict[str, list[TaskResult]] = defaultdict(list)
    for result in results:
        grouped[result.adapter or "unknown"].append(result)

    breakdown: list[AdapterBreakdown] = []
    for adapter_name, adapter_results in sorted(grouped.items()):
        fully = sum(1 for r in adapter_results if r.score >= 1.0)
        near = sum(1 for r in adapter_results if _NEAR_SOLVED_THRESHOLD <= r.score < 1.0)
        failed = sum(1 for r in adapter_results if r.score < _NEAR_SOLVED_THRESHOLD)
        breakdown.append(
            AdapterBreakdown(
                adapter=adapter_name,
                total=len(adapter_results),
                fully_solved=fully,
                near_solved=near,
                failed=failed,
                mean_partial_credit=_mean([r.score for r in adapter_results]),
                cost_per_task=_mean([r.cost_usd for r in adapter_results]),
                time_per_task=_mean([r.duration_seconds for r in adapter_results]),
            )
        )
    return breakdown


def compute_report(results: list[TaskResult]) -> ProgramBenchReport:
    """Compute aggregate metrics from a list of task results.

    Args:
        results: Per-task evaluation outcomes.

    Returns:
        ProgramBenchReport with aggregate statistics.
    """
    if not results:
        return ProgramBenchReport(
            total_tasks=0,
            fully_solved=0,
            near_solved=0,
            failed=0,
            mean_partial_credit=0.0,
            total_cost_usd=0.0,
            time_per_task=0.0,
            median_cost_usd=0.0,
            median_duration_seconds=0.0,
            per_adapter_breakdown=[],
            per_task=[],
        )

    fully = sum(1 for r in results if r.score >= 1.0)
    near = sum(1 for r in results if _NEAR_SOLVED_THRESHOLD <= r.score < 1.0)
    failed = sum(1 for r in results if r.score < _NEAR_SOLVED_THRESHOLD)
    total_cost = sum(r.cost_usd for r in results)

    return ProgramBenchReport(
        total_tasks=len(results),
        fully_solved=fully,
        near_solved=near,
        failed=failed,
        mean_partial_credit=_mean([r.score for r in results]),
        total_cost_usd=total_cost,
        time_per_task=_mean([r.duration_seconds for r in results]),
        median_cost_usd=_median([r.cost_usd for r in results]),
        median_duration_seconds=_median([r.duration_seconds for r in results]),
        per_adapter_breakdown=_compute_adapter_breakdown(results),
        per_task=results.copy(),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _metrics_results_path(sdd_dir: Path) -> Path:
    """Return the canonical JSONL metrics path for ProgramBench runs.

    Args:
        sdd_dir: Project ``.sdd/`` directory (or any root directory).

    Returns:
        Canonical JSONL metrics path.
    """
    return sdd_dir / "metrics" / "programbench_results.jsonl"


def report_to_dict(report: ProgramBenchReport) -> dict[str, Any]:
    """Convert a ProgramBenchReport to a JSON-serialisable dict.

    Args:
        report: The report to serialise.

    Returns:
        Plain JSON-compatible mapping.
    """
    return {
        "run_at": report.run_at,
        "total_tasks": report.total_tasks,
        "fully_solved": report.fully_solved,
        "near_solved": report.near_solved,
        "failed": report.failed,
        "mean_partial_credit": report.mean_partial_credit,
        "total_cost_usd": report.total_cost_usd,
        "time_per_task": report.time_per_task,
        "median_cost_usd": report.median_cost_usd,
        "median_duration_seconds": report.median_duration_seconds,
        "per_adapter_breakdown": [b.to_dict() for b in report.per_adapter_breakdown],
        "per_task": [r.to_dict() for r in report.per_task],
    }


def save_results(report: ProgramBenchReport, sdd_dir: Path) -> Path:
    """Persist ProgramBench results to metrics JSONL plus a JSON snapshot.

    Args:
        report: The aggregate report to persist.
        sdd_dir: Project ``.sdd/`` directory (or any root directory).

    Returns:
        Path to the JSON snapshot.
    """
    data = report_to_dict(report)

    metrics_path = _metrics_results_path(sdd_dir)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    with metrics_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, sort_keys=True))
        handle.write("\n")

    out_dir = sdd_dir / "benchmark"
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = out_dir / "programbench_results.json"
    snapshot_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return snapshot_path


# ---------------------------------------------------------------------------
# Subprocess assert sandbox
# ---------------------------------------------------------------------------


_SANDBOX_DEFAULT_TIMEOUT_SECONDS = 30


_SANDBOX_RUNNER = """\
import json
import sys

payload = json.loads(sys.stdin.read())
setup_code = payload.get("setup_code", "")
candidate_code = payload.get("candidate_code", "")
assert_exprs = payload.get("asserts", [])
task_id = payload.get("task_id", "task")

namespace = {}
error = None
try:
    if setup_code.strip():
        exec(compile(setup_code, "<" + task_id + ":setup>", "exec"), namespace)
    if candidate_code.strip():
        exec(compile(candidate_code, "<" + task_id + ":candidate>", "exec"), namespace)
except BaseException as exc:
    error = "setup/candidate error: " + repr(exc)

passed = 0
if error is None:
    for expr in assert_exprs:
        try:
            result = eval(compile(expr, "<" + task_id + ":assert>", "eval"), namespace)
            if bool(result):
                passed += 1
        except BaseException:
            continue

print(json.dumps({"passed": passed, "total": len(assert_exprs), "error": error}))
"""


def _run_assert_sandbox(
    *,
    task_id: str,
    setup_code: str,
    candidate_code: str,
    asserts: list[str],
    timeout_seconds: int = _SANDBOX_DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, int, str | None]:
    """Evaluate a candidate's asserts in an isolated Python subprocess.

    The runner script reads JSON on stdin, executes the setup and candidate
    code in a fresh namespace, then evaluates each assert expression. The
    harness process never executes candidate code; isolation is provided by
    the subprocess boundary plus the timeout. This mirrors the call shape
    used by :mod:`bernstein.core.sandbox` for runtime evaluation.

    Args:
        task_id: Task identifier used for diagnostic filenames.
        setup_code: Initial setup Python source.
        candidate_code: Candidate Python source.
        asserts: List of Python expressions to evaluate.
        timeout_seconds: Subprocess timeout in seconds.

    Returns:
        Tuple of ``(asserts_passed, asserts_total, error_or_None)``.
    """
    import subprocess
    import sys

    payload = json.dumps(
        {
            "task_id": task_id,
            "setup_code": setup_code,
            "candidate_code": candidate_code,
            "asserts": asserts.copy(),
        }
    )

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _SANDBOX_RUNNER],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 0, len(asserts), "sandbox timeout"
    except OSError as exc:
        return 0, len(asserts), f"sandbox spawn error: {exc!r}"

    if proc.returncode != 0:
        return 0, len(asserts), f"sandbox exit {proc.returncode}: {proc.stderr[:200]}"

    try:
        result_data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        return 0, len(asserts), f"sandbox output parse error: {exc!r}"

    passed = int(result_data.get("passed", 0))
    total = int(result_data.get("total", len(asserts)))
    error: str | None = result_data.get("error")
    return passed, total, error


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class ProgramBenchHarness:
    """Runs Bernstein against ProgramBench tasks and collects metrics.

    The harness reuses the same shape as :class:`bernstein.benchmark.swe_bench.SWEBenchRunner`:
    a separate dataset, a per-task ``run_task`` method, and an aggregating
    ``run`` entry point.

    Args:
        workdir: Project working directory.
        sample: If set, evaluate a random sample of this many tasks.
        task_id: If set, evaluate only this single task.
        subset: Which ProgramBench subset slug to load when lazily downloading.
        seed: Random seed for reproducible sampling.
        dataset_env_var: Optional env var that, when set, points at a local
            ``.jsonl`` dataset file.
    """

    DEFAULT_DATASET_ENV = "BERNSTEIN_PROGRAMBENCH_DATASET"

    def __init__(
        self,
        workdir: Path,
        sample: int | None = None,
        task_id: str | None = None,
        subset: str = "lite",
        seed: int = 42,
        dataset_env_var: str | None = None,
    ) -> None:
        self.workdir = workdir
        self.sample = sample
        self.task_id = task_id
        self.subset = subset
        self._seed = seed
        self.dataset_env_var = dataset_env_var or self.DEFAULT_DATASET_ENV

    # ------------------------------------------------------------------
    # Dataset loading
    # ------------------------------------------------------------------

    def load_dataset(self, dataset_path: Path | None = None) -> list[ProgramBenchTask]:
        """Load ProgramBench tasks from a local JSONL file or stub.

        Resolution order:

        1. Explicit ``dataset_path`` argument if it exists.
        2. The path stored in ``BERNSTEIN_PROGRAMBENCH_DATASET`` env var.
        3. Lazy HuggingFace ``datasets`` download (if available).
        4. Empty list (tests inject tasks directly).

        Args:
            dataset_path: Optional path to a local ``.jsonl`` file.

        Returns:
            Filtered list of :class:`ProgramBenchTask` objects.
        """
        import os

        candidate: Path | None = dataset_path
        if candidate is None:
            env_value = os.environ.get(self.dataset_env_var)
            if env_value:
                from pathlib import Path as _Path

                candidate = _Path(env_value)

        if candidate is not None and candidate.exists():
            tasks: list[ProgramBenchTask] = []
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    tasks.append(ProgramBenchTask.from_dict(raw))
                except (json.JSONDecodeError, KeyError):
                    continue
            return self.filter_tasks(tasks)

        # Lazy HuggingFace download
        try:
            from datasets import load_dataset as hf_load  # type: ignore[import-untyped]

            dataset_name = f"programbench/programbench-{self.subset}"
            raw_dataset: list[Any] = cast(_CAST_LIST_ANY, hf_load(dataset_name, split="test"))
            tasks = [ProgramBenchTask.from_dict(dict(row)) for row in raw_dataset]
            return self.filter_tasks(tasks)
        except ImportError:
            return []

    def filter_tasks(self, tasks: list[ProgramBenchTask]) -> list[ProgramBenchTask]:
        """Apply task_id and sample filters.

        Args:
            tasks: Full list of tasks to filter.

        Returns:
            Filtered (and possibly sampled) list.
        """
        if self.task_id is not None:
            tasks = [t for t in tasks if t.task_id == self.task_id]

        if self.sample is not None and self.sample < len(tasks):
            rng = random.Random(self._seed)
            tasks = rng.sample(tasks, self.sample)

        return tasks

    # ------------------------------------------------------------------
    # Goal construction
    # ------------------------------------------------------------------

    def build_goal(self, task: ProgramBenchTask) -> str:
        """Build a Bernstein goal string from a ProgramBench task.

        Args:
            task: The task to build a goal for.

        Returns:
            Multi-line goal string suitable for ``bernstein --goal``.
        """
        asserts_block = "\n".join(f"  - {a}" for a in task.asserts) or "  (none)"
        return (
            f"ProgramBench task: {task.task_id}\n"
            f"Subset: {task.subset}\n\n"
            f"Description:\n{task.description}\n\n"
            f"Setup:\n{task.setup_code}\n\n"
            f"Target signature:\n{task.target_signature}\n\n"
            f"Runtime asserts (visible):\n{asserts_block}"
        )

    # ------------------------------------------------------------------
    # Sandbox evaluation
    # ------------------------------------------------------------------

    def evaluate_asserts(
        self,
        task: ProgramBenchTask,
        candidate_code: str,
    ) -> tuple[int, int, str | None]:
        """Run a candidate solution against task asserts in a subprocess sandbox.

        Each assert is a Python expression evaluated against a namespace
        containing the candidate's globals plus the task's setup. Evaluation
        runs in a fresh Python interpreter subprocess so the harness process
        is not contaminated by candidate code or imports.

        Args:
            task: The task whose asserts to evaluate.
            candidate_code: Candidate Python code produced by the agent.

        Returns:
            Tuple of ``(asserts_passed, asserts_total, error_or_None)``.
        """
        all_asserts = task.asserts.copy() + task.hidden_asserts.copy()
        if not all_asserts:
            return 0, 0, "no asserts defined"

        return _run_assert_sandbox(
            task_id=task.task_id,
            setup_code=task.setup_code,
            candidate_code=candidate_code,
            asserts=all_asserts,
        )

    # ------------------------------------------------------------------
    # Adapter invocation
    # ------------------------------------------------------------------

    def _invoke_adapter(
        self,
        adapter: str,
        task: ProgramBenchTask,
    ) -> tuple[str, float, float]:
        """Invoke an adapter to produce a candidate for a task.

        Default behaviour shells out to ``bernstein --goal ... --adapter ...``.
        Tests typically override this method.

        Args:
            adapter: Adapter slug (e.g. ``"claude"``).
            task: The task to solve.

        Returns:
            Tuple of ``(candidate_code, cost_usd, duration_seconds)``.

        Raises:
            RuntimeError: If the subprocess fails or times out.
        """
        import subprocess

        goal = self.build_goal(task)
        t0 = time.monotonic()

        proc = subprocess.run(
            [
                "bernstein",
                "--goal",
                goal,
                "--adapter",
                adapter,
                "--headless",
                "--budget",
                "2.00",
            ],
            cwd=self.workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )

        duration = time.monotonic() - t0

        if proc.returncode != 0:
            raise RuntimeError(f"Bernstein exited {proc.returncode}: {proc.stderr[:200]}")

        candidate_path = self.workdir / ".sdd" / "benchmark" / f"{task.task_id}.py"
        candidate_code = candidate_path.read_text(encoding="utf-8") if candidate_path.exists() else ""

        cost_usd = self._read_run_cost()
        return candidate_code, cost_usd, duration

    def _read_run_cost(self) -> float:
        """Read total cost of the last Bernstein run from metrics JSONL files."""
        metrics_dir = self.workdir / ".sdd" / "metrics"
        if not metrics_dir.exists():
            return 0.0
        total = 0.0
        for jsonl_file in metrics_dir.glob("cost_efficiency_*.jsonl"):
            for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                    total += float(record.get("cost_usd", 0.0))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        return total

    # ------------------------------------------------------------------
    # Public: run a single task
    # ------------------------------------------------------------------

    def run_task(self, adapter: str, task: ProgramBenchTask) -> TaskResult:
        """Evaluate Bernstein on a single ProgramBench task.

        Args:
            adapter: Adapter slug used for the invocation.
            task: The task to solve.

        Returns:
            :class:`TaskResult` with score and metrics.
        """
        try:
            candidate_code, cost_usd, duration = self._invoke_adapter(adapter, task)
        except Exception as exc:
            return TaskResult(
                task_id=task.task_id,
                score=0.0,
                asserts_passed=0,
                asserts_total=len(task.asserts) + len(task.hidden_asserts),
                fully_solved=False,
                cost_usd=0.0,
                duration_seconds=0.0,
                adapter=adapter,
                log_path=None,
                error=str(exc),
            )

        passed, total, sandbox_err = self.evaluate_asserts(task, candidate_code)
        score = compute_score(passed, total)
        return TaskResult(
            task_id=task.task_id,
            score=score,
            asserts_passed=passed,
            asserts_total=total,
            fully_solved=score >= 1.0,
            cost_usd=cost_usd,
            duration_seconds=duration,
            adapter=adapter,
            log_path=None,
            error=sandbox_err,
        )

    # ------------------------------------------------------------------
    # Public: run the suite
    # ------------------------------------------------------------------

    def run(
        self,
        adapter: str,
        tasks: list[ProgramBenchTask] | None = None,
        subset: str | None = None,
        dataset_path: Path | None = None,
    ) -> ProgramBenchReport:
        """Run Bernstein against all (or a filtered subset of) ProgramBench tasks.

        Args:
            adapter: Adapter slug used for each task.
            tasks: Pre-loaded tasks to evaluate. If None, calls
                :meth:`load_dataset`.
            subset: Optional override for the subset slug.
            dataset_path: Passed to :meth:`load_dataset` if ``tasks`` is None.

        Returns:
            Aggregate :class:`ProgramBenchReport`.
        """
        if subset is not None:
            self.subset = subset
        if tasks is None:
            tasks = self.load_dataset(dataset_path)

        results = [self.run_task(adapter, task) for task in tasks]
        return compute_report(results)
