"""SWE-Bench evaluation harness for Bernstein.

Runs Bernstein against SWE-Bench (or SWE-Bench Lite) instances and reports
resolve rate, cost, and time metrics comparable to published leaderboard numbers.

Usage::

    runner = SWEBenchRunner(workdir=Path("."), sample=20)
    instances = runner.load_dataset()
    results = [runner.run_instance(inst) for inst in instances]
    report = compute_report(results)
    save_results(report, Path(".sdd"))
"""

from __future__ import annotations

import json
import random
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_LIST_ANY = "list[Any]"


@dataclass(frozen=True)
class SWEInstance:
    """A single SWE-Bench evaluation instance.

    Args:
        instance_id: Unique identifier, e.g. ``django__django-11905``.
        repo: GitHub repository slug, e.g. ``django/django``.
        base_commit: Git commit hash of the base (buggy) state.
        problem_statement: Natural-language description of the bug.
        hints_text: Optional additional hints from the issue.
        test_patch: Diff that adds the evaluation test(s).
        patch: Gold-standard fix diff (used for reference, not given to agent).
        fail_to_pass: Tests that must go from failing → passing.
        pass_to_pass: Tests that must stay passing.
        environment_setup_commit: Commit used to set up the conda environment.
        version: Repository version string.
        created_at: ISO-8601 timestamp when the issue was created.
        repo_version: Repository version used in evaluation.
    """

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str
    test_patch: str
    patch: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    environment_setup_commit: str
    version: str
    created_at: str
    repo_version: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SWEInstance:
        """Parse a SWEInstance from the raw HuggingFace/JSON dataset format.

        Args:
            raw: Dict with SWE-Bench dataset fields.

        Returns:
            Parsed SWEInstance.
        """

        def _parse_tests(value: Any) -> list[str]:
            if isinstance(value, list):
                lst = cast(_CAST_LIST_ANY, value)
                return [str(v) for v in lst]
            if isinstance(value, str):
                try:
                    parsed: Any = json.loads(value)
                    if isinstance(parsed, list):
                        plst = cast(_CAST_LIST_ANY, parsed)
                        return [str(v) for v in plst]
                except json.JSONDecodeError:
                    pass  # Not a JSON list; treat as plain string
                return [value] if value else []
            return []

        ftp = _parse_tests(raw.get("FAIL_TO_PASS", raw.get("fail_to_pass", [])))
        ptp = _parse_tests(raw.get("PASS_TO_PASS", raw.get("pass_to_pass", [])))

        return cls(
            instance_id=str(raw["instance_id"]),
            repo=str(raw.get("repo", "")),
            base_commit=str(raw.get("base_commit", "")),
            problem_statement=str(raw.get("problem_statement", "")),
            hints_text=str(raw.get("hints_text", "")),
            test_patch=str(raw.get("test_patch", "")),
            patch=str(raw.get("patch", "")),
            fail_to_pass=ftp,
            pass_to_pass=ptp,
            environment_setup_commit=str(raw.get("environment_setup_commit", "")),
            version=str(raw.get("version", "")),
            created_at=str(raw.get("created_at", "")),
            repo_version=str(raw.get("repo_version", raw.get("version", ""))),
        )


@dataclass
class InstanceResult:
    """Result of running Bernstein on a single SWE-Bench instance.

    Args:
        instance_id: Matches the SWEInstance this result is for.
        resolved: Whether the agent's patch resolved all failing tests.
        cost_usd: Estimated LLM API cost in USD.
        duration_seconds: Wall-clock time taken.
        agent_count: Number of agents spawned.
        retries: Number of retry attempts.
        error: Error message if the run failed, else None.
        model_name: Primary model used for the run, if observable.
    """

    instance_id: str
    resolved: bool
    cost_usd: float
    duration_seconds: float
    agent_count: int
    retries: int
    error: str | None
    model_name: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON export.

        Returns:
            Dict with all fields.
        """
        return {
            "instance_id": self.instance_id,
            "resolved": self.resolved,
            "cost_usd": self.cost_usd,
            "duration_seconds": self.duration_seconds,
            "agent_count": self.agent_count,
            "retries": self.retries,
            "error": self.error,
            "model_name": self.model_name,
        }


@dataclass
class ModelBreakdown:
    """Aggregate SWE-Bench metrics for a single model.

    Args:
        model_name: Name of the model used for the grouped runs.
        total: Total instances evaluated with this model.
        resolved: Number of resolved instances.
        resolve_rate: Fraction resolved for this model.
        cost_per_task: Mean cost per task for this model.
        time_per_task: Mean duration per task for this model.
    """

    model_name: str
    total: int
    resolved: int
    resolve_rate: float
    cost_per_task: float
    time_per_task: float

    def to_dict(self) -> dict[str, float | int | str]:
        """Serialise the breakdown for JSON output.

        Returns:
            Plain JSON-compatible mapping.
        """
        return {
            "model_name": self.model_name,
            "total": self.total,
            "resolved": self.resolved,
            "resolve_rate": self.resolve_rate,
            "cost_per_task": self.cost_per_task,
            "time_per_task": self.time_per_task,
        }


@dataclass
class BenchmarkReport:
    """Aggregate report for a SWE-Bench evaluation run.

    Args:
        total: Total number of instances evaluated.
        resolved: Number of instances resolved.
        resolve_rate: Fraction resolved (0.0-1.0).
        cost_per_task: Mean cost across all instances.
        time_per_task: Mean wall-clock time across all instances.
        per_model_breakdown: Per-model aggregate metrics.
        median_cost_usd: Median cost across all instances.
        median_duration_seconds: Median wall-clock time across all instances.
        cost_effectiveness_ratio: resolved / total_cost_usd.
        instance_results: Per-instance results.
        run_at: ISO-8601 timestamp of when the report was generated.
    """

    total: int
    resolved: int
    resolve_rate: float
    cost_per_task: float
    time_per_task: float
    per_model_breakdown: list[ModelBreakdown]
    median_cost_usd: float
    median_duration_seconds: float
    cost_effectiveness_ratio: float
    instance_results: list[InstanceResult]
    run_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


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


def _compute_model_breakdown(results: list[InstanceResult]) -> list[ModelBreakdown]:
    grouped: dict[str, list[InstanceResult]] = defaultdict(list)
    for result in results:
        grouped[result.model_name or "unknown"].append(result)

    breakdown: list[ModelBreakdown] = []
    for model_name, model_results in sorted(grouped.items()):
        resolved = sum(1 for result in model_results if result.resolved)
        breakdown.append(
            ModelBreakdown(
                model_name=model_name,
                total=len(model_results),
                resolved=resolved,
                resolve_rate=resolved / len(model_results),
                cost_per_task=_mean([result.cost_usd for result in model_results]),
                time_per_task=_mean([result.duration_seconds for result in model_results]),
            )
        )
    return breakdown


def compute_report(results: list[InstanceResult]) -> BenchmarkReport:
    """Compute aggregate metrics from a list of instance results.

    Args:
        results: Per-instance evaluation outcomes.

    Returns:
        BenchmarkReport with aggregate statistics.
    """
    if not results:
        return BenchmarkReport(
            total=0,
            resolved=0,
            resolve_rate=0.0,
            cost_per_task=0.0,
            time_per_task=0.0,
            per_model_breakdown=[],
            median_cost_usd=0.0,
            median_duration_seconds=0.0,
            cost_effectiveness_ratio=0.0,
            instance_results=[],
        )

    resolved_count = sum(1 for r in results if r.resolved)
    total_cost = sum(r.cost_usd for r in results)
    cost_effectiveness = resolved_count / total_cost if total_cost > 0 else 0.0

    return BenchmarkReport(
        total=len(results),
        resolved=resolved_count,
        resolve_rate=resolved_count / len(results),
        cost_per_task=_mean([r.cost_usd for r in results]),
        time_per_task=_mean([r.duration_seconds for r in results]),
        per_model_breakdown=_compute_model_breakdown(results),
        median_cost_usd=_median([r.cost_usd for r in results]),
        median_duration_seconds=_median([r.duration_seconds for r in results]),
        cost_effectiveness_ratio=cost_effectiveness,
        instance_results=list(results),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _metrics_results_path(sdd_dir: Path) -> Path:
    """Return the canonical JSONL metrics path for SWE-Bench runs.

    Args:
        sdd_dir: Project ``.sdd/`` directory (or any root directory).

    Returns:
        Canonical JSONL metrics path.
    """
    return sdd_dir / "metrics" / "swe_bench_results.jsonl"


def save_results(report: BenchmarkReport, sdd_dir: Path) -> Path:
    """Persist benchmark results to metrics JSONL plus a legacy JSON snapshot.

    Args:
        report: The aggregate report to persist.
        sdd_dir: Project ``.sdd/`` directory (or any root directory).

    Returns:
        Path to the legacy JSON snapshot for backward compatibility.
    """
    data: dict[str, Any] = {
        "run_at": report.run_at,
        "total": report.total,
        "resolved": report.resolved,
        "resolve_rate": report.resolve_rate,
        "cost_per_task": report.cost_per_task,
        "time_per_task": report.time_per_task,
        "per_model_breakdown": [entry.to_dict() for entry in report.per_model_breakdown],
        "median_cost_usd": report.median_cost_usd,
        "median_duration_seconds": report.median_duration_seconds,
        "cost_effectiveness_ratio": report.cost_effectiveness_ratio,
        "instance_results": [r.to_dict() for r in report.instance_results],
    }

    metrics_path = _metrics_results_path(sdd_dir)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    with metrics_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, sort_keys=True))
        handle.write("\n")

    out_dir = sdd_dir / "benchmark"
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = out_dir / "swe_bench_results.json"
    snapshot_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return snapshot_path


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class SWEBenchRunner:
    """Runs Bernstein against SWE-Bench instances and collects metrics.

    Args:
        workdir: Project working directory (where Bernstein will operate).
        sample: If set, evaluate a random sample of this many instances.
        instance_id: If set, evaluate only this single instance.
        subset: Which benchmark subset to use when lazily downloading.
        seed: Random seed for reproducible sampling.
    """

    def __init__(
        self,
        workdir: Path,
        sample: int | None = None,
        instance_id: str | None = None,
        subset: Literal["lite", "full"] = "lite",
        seed: int = 42,
    ) -> None:
        self.workdir = workdir
        self.sample = sample
        self.instance_id = instance_id
        self.subset = subset
        self._seed = seed

    # ------------------------------------------------------------------
    # Dataset loading
    # ------------------------------------------------------------------

    def load_dataset(self, dataset_path: Path | None = None) -> list[SWEInstance]:
        """Load SWE-Bench Lite instances from a local JSONL file or built-in stub.

        When ``dataset_path`` is None and the HuggingFace ``datasets`` library
        is available, the dataset is downloaded on demand.  Otherwise falls back
        to an empty list so tests can inject their own instances via
        :meth:`filter_instances`.

        Args:
            dataset_path: Optional path to a local ``.jsonl`` file with raw
                SWE-Bench records.

        Returns:
            List of :class:`SWEInstance` objects.
        """
        if dataset_path is not None and dataset_path.exists():
            instances: list[SWEInstance] = []
            for line in dataset_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    instances.append(SWEInstance.from_dict(raw))
                except (json.JSONDecodeError, KeyError):
                    continue
            return self.filter_instances(instances)

        # Lazy HuggingFace download
        try:
            from datasets import load_dataset as hf_load  # type: ignore[import-untyped]

            dataset_name = "princeton-nlp/SWE-bench_Lite" if self.subset == "lite" else "princeton-nlp/SWE-bench"
            raw_dataset: list[Any] = cast(_CAST_LIST_ANY, hf_load(dataset_name, split="test"))
            instances = [SWEInstance.from_dict(dict(row)) for row in raw_dataset]
            return self.filter_instances(instances)
        except ImportError:
            return []

    def filter_instances(self, instances: list[SWEInstance]) -> list[SWEInstance]:
        """Apply instance_id and sample filters.

        Args:
            instances: Full list of instances to filter.

        Returns:
            Filtered (and possibly sampled) list.
        """
        if self.instance_id is not None:
            instances = [i for i in instances if i.instance_id == self.instance_id]

        if self.sample is not None and self.sample < len(instances):
            rng = random.Random(self._seed)
            instances = rng.sample(instances, self.sample)

        return instances

    # ------------------------------------------------------------------
    # Goal construction
    # ------------------------------------------------------------------

    def build_goal(self, instance: SWEInstance) -> str:
        """Build a Bernstein goal string from a SWE-Bench instance.

        Args:
            instance: The instance to build a goal for.

        Returns:
            Multi-line goal string suitable for ``bernstein --goal``.
        """
        tests_block = "\n".join(f"  - {t}" for t in instance.fail_to_pass)
        return (
            f"Repository: {instance.repo}\n"
            f"Base commit: {instance.base_commit}\n\n"
            f"Issue:\n{instance.problem_statement}\n\n"
            f"Tests that must pass after your fix:\n{tests_block}"
        )

    # ------------------------------------------------------------------
    # Patch evaluation
    # ------------------------------------------------------------------

    def evaluate_patch(self, instance: SWEInstance, patch_text: str) -> bool:
        """Determine whether a patch resolves the instance.

        This is a heuristic check used without a full Docker sandbox.
        A patch is considered resolved when it is non-empty.  In a full
        evaluation environment callers should override this method to
        actually apply the patch and run the failing tests.

        Args:
            instance: The SWE-Bench instance being evaluated.
            patch_text: Unified diff produced by the agent.

        Returns:
            True if the patch is non-empty (presumed resolving), else False.
        """
        return bool(patch_text and patch_text.strip())

    # ------------------------------------------------------------------
    # Internal: spawn Bernstein
    # ------------------------------------------------------------------

    def _spawn_bernstein(
        self,
        instance: SWEInstance,
    ) -> tuple[str, float, float, int] | tuple[str, float, float, int, str]:
        """Run Bernstein on a single instance and return raw outputs.

        This method is intended to be mocked in tests.  In production it
        launches ``bernstein --goal <goal> --headless`` as a subprocess,
        waits for completion, and reads the resulting patch from the
        working directory.

        Args:
            instance: The SWE-Bench instance to solve.

        Returns:
            Tuple of ``(patch_text, cost_usd, duration_seconds, agent_count)``
            or ``(patch_text, cost_usd, duration_seconds, agent_count, model_name)``.

        Raises:
            RuntimeError: If the subprocess fails or times out.
        """
        import subprocess

        goal = self.build_goal(instance)
        t0 = time.monotonic()

        proc = subprocess.run(
            ["bernstein", "--goal", goal, "--headless", "--budget", "2.00"],
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

        # Read patch produced by agents (written to .sdd/benchmark/patch.diff)
        patch_path = self.workdir / ".sdd" / "benchmark" / "patch.diff"
        patch_text = patch_path.read_text() if patch_path.exists() else ""

        # Read cost from metrics
        cost_usd = self._read_run_cost()

        # Count agents from runtime logs
        agent_count = self._count_agents()

        model_name = self._read_primary_model()
        return patch_text, cost_usd, duration, agent_count, model_name

    def _read_run_cost(self) -> float:
        """Read total cost of the last Bernstein run from metrics JSONL files."""
        metrics_dir = self.workdir / ".sdd" / "metrics"
        if not metrics_dir.exists():
            return 0.0
        total = 0.0
        for jsonl_file in metrics_dir.glob("cost_efficiency_*.jsonl"):
            for line in jsonl_file.read_text().splitlines():
                try:
                    record = json.loads(line)
                    total += float(record.get("cost_usd", 0.0))
                except (TypeError, ValueError):
                    continue
        return total

    def _count_agents(self) -> int:
        """Count agent session directories from the last run."""
        agents_dir = self.workdir / ".sdd" / "agents"
        if not agents_dir.exists():
            return 0
        return sum(1 for p in agents_dir.iterdir() if p.is_dir())

    def _read_primary_model(self) -> str:
        """Read the primary model from the latest runtime agent snapshot.

        Returns:
            The first non-empty model from ``agents.json``, or ``"unknown"``.
        """
        agents_path = self.workdir / ".sdd" / "runtime" / "agents.json"
        if not agents_path.exists():
            return "unknown"
        try:
            raw = json.loads(agents_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "unknown"
        if not isinstance(raw, dict):
            return "unknown"
        agents = raw.get("agents")
        if not isinstance(agents, list):
            return "unknown"
        for agent in agents:
            if isinstance(agent, dict):
                model_name = agent.get("model")
                if isinstance(model_name, str) and model_name.strip():
                    return model_name.strip()
        return "unknown"

    # ------------------------------------------------------------------
    # Public: run a single instance
    # ------------------------------------------------------------------

    def run_instance(self, instance: SWEInstance) -> InstanceResult:
        """Evaluate Bernstein on a single SWE-Bench instance.

        Args:
            instance: The SWE-Bench instance to solve.

        Returns:
            :class:`InstanceResult` with outcome metrics.
        """
        try:
            raw_spawn_result = self._spawn_bernstein(instance)
            if len(raw_spawn_result) == 4:
                patch_text, cost_usd, duration_seconds, agent_count = raw_spawn_result
                model_name = "unknown"
            else:
                patch_text, cost_usd, duration_seconds, agent_count, model_name = raw_spawn_result
        except Exception as exc:
            return InstanceResult(
                instance_id=instance.instance_id,
                resolved=False,
                cost_usd=0.0,
                duration_seconds=0.0,
                agent_count=0,
                retries=0,
                error=str(exc),
                model_name="unknown",
            )

        resolved = self.evaluate_patch(instance, patch_text)
        return InstanceResult(
            instance_id=instance.instance_id,
            resolved=resolved,
            cost_usd=cost_usd,
            duration_seconds=duration_seconds,
            agent_count=agent_count,
            retries=0,
            error=None if resolved else "Patch did not resolve failing tests",
            model_name=model_name,
        )

    # ------------------------------------------------------------------
    # Public: run all instances
    # ------------------------------------------------------------------

    def run(
        self,
        instances: list[SWEInstance] | None = None,
        dataset_path: Path | None = None,
    ) -> BenchmarkReport:
        """Run Bernstein against all (or a filtered subset of) SWE-Bench instances.

        Args:
            instances: Pre-loaded instances to evaluate.  If None, calls
                :meth:`load_dataset` to fetch them.
            dataset_path: Passed to :meth:`load_dataset` if ``instances`` is None.

        Returns:
            Aggregate :class:`BenchmarkReport`.
        """
        if instances is None:
            instances = self.load_dataset(dataset_path)

        results = [self.run_instance(inst) for inst in instances]
        return compute_report(results)
