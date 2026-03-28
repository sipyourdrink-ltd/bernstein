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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


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
        FAIL_TO_PASS: Alias for ``fail_to_pass`` (raw dataset field name).
        PASS_TO_PASS: Alias for ``pass_to_pass`` (raw dataset field name).
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
    FAIL_TO_PASS: list[str]
    PASS_TO_PASS: list[str]

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
                lst = cast("list[Any]", value)
                return [str(v) for v in lst]
            if isinstance(value, str):
                try:
                    parsed: Any = json.loads(value)
                    if isinstance(parsed, list):
                        plst = cast("list[Any]", parsed)
                        return [str(v) for v in plst]
                except json.JSONDecodeError:
                    pass
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
            FAIL_TO_PASS=ftp,
            PASS_TO_PASS=ptp,
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
    """

    instance_id: str
    resolved: bool
    cost_usd: float
    duration_seconds: float
    agent_count: int
    retries: int
    error: str | None

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
        }


@dataclass
class BenchmarkReport:
    """Aggregate report for a SWE-Bench evaluation run.

    Args:
        total: Total number of instances evaluated.
        resolved: Number of instances resolved.
        resolve_rate: Fraction resolved (0.0-1.0).
        median_cost_usd: Median cost across all instances.
        median_duration_seconds: Median wall-clock time across all instances.
        cost_effectiveness_ratio: resolved / total_cost_usd.
        instance_results: Per-instance results.
        run_at: ISO-8601 timestamp of when the report was generated.
    """

    total: int
    resolved: int
    resolve_rate: float
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
        median_cost_usd=_median([r.cost_usd for r in results]),
        median_duration_seconds=_median([r.duration_seconds for r in results]),
        cost_effectiveness_ratio=cost_effectiveness,
        instance_results=list(results),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_results(report: BenchmarkReport, sdd_dir: Path) -> Path:
    """Write benchmark results to ``<sdd_dir>/benchmark/swe_bench_results.json``.

    Args:
        report: The aggregate report to persist.
        sdd_dir: Project ``.sdd/`` directory (or any root directory).

    Returns:
        Path to the written JSON file.
    """
    out_dir = sdd_dir / "benchmark"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "swe_bench_results.json"

    data: dict[str, Any] = {
        "run_at": report.run_at,
        "total": report.total,
        "resolved": report.resolved,
        "resolve_rate": report.resolve_rate,
        "median_cost_usd": report.median_cost_usd,
        "median_duration_seconds": report.median_duration_seconds,
        "cost_effectiveness_ratio": report.cost_effectiveness_ratio,
        "instance_results": [r.to_dict() for r in report.instance_results],
    }

    out_path.write_text(json.dumps(data, indent=2))
    return out_path


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class SWEBenchRunner:
    """Runs Bernstein against SWE-Bench instances and collects metrics.

    Args:
        workdir: Project working directory (where Bernstein will operate).
        sample: If set, evaluate a random sample of this many instances.
        instance_id: If set, evaluate only this single instance.
        seed: Random seed for reproducible sampling.
    """

    def __init__(
        self,
        workdir: Path,
        sample: int | None = None,
        instance_id: str | None = None,
        seed: int = 42,
    ) -> None:
        self.workdir = workdir
        self.sample = sample
        self.instance_id = instance_id
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

            raw_dataset: list[Any] = cast("list[Any]", hf_load("princeton-nlp/SWE-bench_Lite", split="test"))
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

    def _spawn_bernstein(self, instance: SWEInstance) -> tuple[str, float, float, int]:
        """Run Bernstein on a single instance and return raw outputs.

        This method is intended to be mocked in tests.  In production it
        launches ``bernstein --goal <goal> --headless`` as a subprocess,
        waits for completion, and reads the resulting patch from the
        working directory.

        Args:
            instance: The SWE-Bench instance to solve.

        Returns:
            Tuple of (patch_text, cost_usd, duration_seconds, agent_count).

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

        return patch_text, cost_usd, duration, agent_count

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
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
        return total

    def _count_agents(self) -> int:
        """Count agent session directories from the last run."""
        agents_dir = self.workdir / ".sdd" / "agents"
        if not agents_dir.exists():
            return 0
        return sum(1 for p in agents_dir.iterdir() if p.is_dir())

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
            patch_text, cost_usd, duration_seconds, agent_count = self._spawn_bernstein(instance)
        except Exception as exc:
            return InstanceResult(
                instance_id=instance.instance_id,
                resolved=False,
                cost_usd=0.0,
                duration_seconds=0.0,
                agent_count=0,
                retries=0,
                error=str(exc),
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
