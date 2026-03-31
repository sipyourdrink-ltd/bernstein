"""Benchmark regression gate: block merge when performance degrades beyond threshold."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bernstein.core.git_basic import run_git

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BenchmarkMetrics:
    """Performance metrics extracted from a single benchmark."""

    mean_s: float
    """Mean execution time in seconds (lower is better)."""

    ops: float | None
    """Operations per second (higher is better)."""

    memory_mb: float | None
    """Peak memory usage in MB (lower is better)."""


@dataclass(frozen=True)
class BenchmarkRegression:
    """A detected regression for one benchmark/metric pair."""

    name: str
    """Benchmark name."""

    metric: str
    """Which metric regressed: 'mean_s', 'ops', or 'memory_mb'."""

    baseline: float
    current: float
    delta_pct: float
    """Regression magnitude as a percentage (positive = worse)."""


@dataclass(frozen=True)
class BenchmarkEvaluation:
    """Outcome of a baseline-vs-current benchmark comparison."""

    passed: bool
    regressions: list[BenchmarkRegression]
    detail: str
    baseline_metrics: dict[str, BenchmarkMetrics]
    current_metrics: dict[str, BenchmarkMetrics]


class BenchmarkGate:
    """Block merge if performance benchmarks regress beyond the configured threshold.

    Expects the benchmark command to produce a JSON results file in
    pytest-benchmark format at ``.benchmark_results.json`` relative to the
    working directory.  Each entry must have a ``stats.mean`` field (seconds);
    ``stats.ops`` (throughput) and ``stats.memory_mb`` are optional.

    A regression is detected when:
    - ``mean_s`` increases by more than ``threshold`` (slower)
    - ``ops`` decreases by more than ``threshold`` (lower throughput)
    - ``memory_mb`` increases by more than ``threshold`` (higher memory)
    """

    BASELINE_FILE = Path(".sdd/cache/benchmark_baseline.json")
    RESULTS_FILE = ".benchmark_results.json"
    DEFAULT_COMMAND = "uv run pytest benchmarks/ --benchmark-json=.benchmark_results.json -q"

    def __init__(
        self,
        workdir: Path,
        run_dir: Path,
        *,
        base_ref: str = "main",
        benchmark_command: str | None = None,
        threshold: float = 0.15,
    ) -> None:
        self._workdir = workdir
        self._run_dir = run_dir
        self._base_ref = base_ref
        self._benchmark_command = benchmark_command or self.DEFAULT_COMMAND
        self._threshold = threshold
        self._baseline_path = workdir / self.BASELINE_FILE

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def measure_baseline(self) -> dict[str, BenchmarkMetrics]:
        """Measure benchmarks on the configured base ref in a temporary worktree."""
        temp_parent = self._workdir / ".sdd" / "tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="benchmark-base-", dir=temp_parent) as temp_dir:
            temp_path = Path(temp_dir)
            add_result = run_git(
                ["worktree", "add", "--detach", str(temp_path), self._base_ref],
                self._workdir,
                timeout=60,
            )
            if not add_result.ok:
                raise RuntimeError(add_result.stderr.strip() or f"Failed to create worktree for {self._base_ref}")
            try:
                return self._run_measurement(temp_path)
            finally:
                remove_result = run_git(
                    ["worktree", "remove", "--force", str(temp_path)],
                    self._workdir,
                    timeout=60,
                )
                if not remove_result.ok:
                    logger.warning(
                        "Failed to remove temporary worktree %s: %s",
                        temp_path,
                        remove_result.stderr.strip(),
                    )
                    shutil.rmtree(temp_path, ignore_errors=True)

    def measure_current(self) -> dict[str, BenchmarkMetrics]:
        """Measure benchmarks on the current run directory."""
        return self._run_measurement(self._run_dir)

    def evaluate(self) -> BenchmarkEvaluation:
        """Compare current benchmarks to the cached or freshly measured baseline."""
        baseline = self._load_or_measure_baseline()
        current = self.measure_current()
        regressions = self._detect_regressions(baseline, current)
        passed = len(regressions) == 0
        detail = self._format_detail(baseline, current, regressions)
        return BenchmarkEvaluation(
            passed=passed,
            regressions=regressions,
            detail=detail,
            baseline_metrics=baseline,
            current_metrics=current,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_or_measure_baseline(self) -> dict[str, BenchmarkMetrics]:
        """Load the cached baseline when compatible, else re-measure it."""
        if self._baseline_path.exists():
            try:
                raw: object = json.loads(self._baseline_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict):
                data = cast("dict[str, object]", raw)
                if data.get("base_ref") == self._base_ref and data.get("benchmark_command") == self._benchmark_command:
                    metrics_raw = data.get("metrics")
                    if isinstance(metrics_raw, dict):
                        try:
                            return self._deserialize_metrics(cast("dict[str, object]", metrics_raw))
                        except (KeyError, ValueError, TypeError):
                            pass

        baseline = self.measure_baseline()
        self._baseline_path.parent.mkdir(parents=True, exist_ok=True)
        self._baseline_path.write_text(
            json.dumps(
                {
                    "base_ref": self._base_ref,
                    "benchmark_command": self._benchmark_command,
                    "metrics": self._serialize_metrics(baseline),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return baseline

    def _run_measurement(self, cwd: Path) -> dict[str, BenchmarkMetrics]:
        """Execute the benchmark command in ``cwd`` and parse results."""
        result = subprocess.run(
            self._benchmark_command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            combined = (result.stdout + result.stderr).strip()
            raise RuntimeError(combined or "Benchmark command failed")
        return self._parse_results(cwd)

    def _parse_results(self, cwd: Path) -> dict[str, BenchmarkMetrics]:
        """Parse benchmark results from the generated JSON file."""
        results_path = cwd / self.RESULTS_FILE
        if not results_path.exists():
            raise RuntimeError(f"Benchmark results file not found: {results_path}")
        try:
            raw: object = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Failed to read benchmark results: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("Benchmark results have invalid structure")
        return self._extract_metrics(cast("dict[str, object]", raw))

    def _extract_metrics(self, data: dict[str, object]) -> dict[str, BenchmarkMetrics]:
        """Extract BenchmarkMetrics from pytest-benchmark JSON format."""
        benchmarks_raw = data.get("benchmarks")
        if not isinstance(benchmarks_raw, list):
            raise RuntimeError("Benchmark results missing 'benchmarks' list")
        benchmarks_list = cast("list[object]", benchmarks_raw)

        metrics: dict[str, BenchmarkMetrics] = {}
        for bench_item in benchmarks_list:
            if not isinstance(bench_item, dict):
                continue
            bench_map = cast("dict[str, object]", bench_item)
            name = bench_map.get("name")
            if not isinstance(name, str):
                continue
            stats_raw = bench_map.get("stats")
            if not isinstance(stats_raw, dict):
                continue
            stats = cast("dict[str, object]", stats_raw)
            mean_raw = stats.get("mean")
            if not isinstance(mean_raw, (int, float)):
                continue
            ops_raw = stats.get("ops")
            ops = float(ops_raw) if isinstance(ops_raw, (int, float)) else None
            mem_raw = stats.get("memory_mb")
            memory_mb = float(mem_raw) if isinstance(mem_raw, (int, float)) else None
            metrics[name] = BenchmarkMetrics(
                mean_s=float(mean_raw),
                ops=ops,
                memory_mb=memory_mb,
            )

        if not metrics:
            raise RuntimeError("No benchmarks found in results")
        return metrics

    def _detect_regressions(
        self,
        baseline: dict[str, BenchmarkMetrics],
        current: dict[str, BenchmarkMetrics],
    ) -> list[BenchmarkRegression]:
        """Detect regressions that exceed the configured threshold."""
        regressions: list[BenchmarkRegression] = []
        for name, curr in current.items():
            base = baseline.get(name)
            if base is None:
                continue
            # mean_s: an increase is a regression (slower)
            if base.mean_s > 0:
                delta = (curr.mean_s - base.mean_s) / base.mean_s
                if delta > self._threshold:
                    regressions.append(
                        BenchmarkRegression(
                            name=name,
                            metric="mean_s",
                            baseline=base.mean_s,
                            current=curr.mean_s,
                            delta_pct=round(delta * 100, 2),
                        )
                    )
            # ops: a decrease is a regression (lower throughput)
            if curr.ops is not None and base.ops is not None and base.ops > 0:
                delta = (base.ops - curr.ops) / base.ops
                if delta > self._threshold:
                    regressions.append(
                        BenchmarkRegression(
                            name=name,
                            metric="ops",
                            baseline=base.ops,
                            current=curr.ops,
                            delta_pct=round(delta * 100, 2),
                        )
                    )
            # memory_mb: an increase is a regression (more memory)
            if curr.memory_mb is not None and base.memory_mb is not None and base.memory_mb > 0:
                delta = (curr.memory_mb - base.memory_mb) / base.memory_mb
                if delta > self._threshold:
                    regressions.append(
                        BenchmarkRegression(
                            name=name,
                            metric="memory_mb",
                            baseline=base.memory_mb,
                            current=curr.memory_mb,
                            delta_pct=round(delta * 100, 2),
                        )
                    )
        return regressions

    def _format_detail(
        self,
        baseline: dict[str, BenchmarkMetrics],
        current: dict[str, BenchmarkMetrics],
        regressions: list[BenchmarkRegression],
    ) -> str:
        """Format a human-readable summary of the benchmark comparison."""
        if not regressions:
            bench_count = len(current)
            return f"All {bench_count} benchmark(s) within {self._threshold:.0%} regression threshold."
        lines = [f"Performance regression detected (threshold: {self._threshold:.0%}):"]
        for reg in regressions:
            if reg.metric == "mean_s":
                lines.append(
                    f"  {reg.name}: response time {reg.baseline:.6f}s -> {reg.current:.6f}s (+{reg.delta_pct:.1f}%)"
                )
            elif reg.metric == "ops":
                lines.append(
                    f"  {reg.name}: throughput {reg.baseline:.1f} -> {reg.current:.1f} ops/s (-{reg.delta_pct:.1f}%)"
                )
            elif reg.metric == "memory_mb":
                lines.append(
                    f"  {reg.name}: memory {reg.baseline:.1f}MB -> {reg.current:.1f}MB (+{reg.delta_pct:.1f}%)"
                )
        return "\n".join(lines)

    def _serialize_metrics(self, metrics: dict[str, BenchmarkMetrics]) -> dict[str, object]:
        """Serialize metrics to a JSON-compatible dict."""
        return {
            name: {
                "mean_s": m.mean_s,
                "ops": m.ops,
                "memory_mb": m.memory_mb,
            }
            for name, m in metrics.items()
        }

    def _deserialize_metrics(self, data: dict[str, object]) -> dict[str, BenchmarkMetrics]:
        """Deserialize metrics from a cached JSON dict."""
        metrics: dict[str, BenchmarkMetrics] = {}
        for name, raw in data.items():
            if not isinstance(raw, dict):
                continue
            entry = cast("dict[str, object]", raw)
            mean_s_raw = entry.get("mean_s")
            if not isinstance(mean_s_raw, (int, float)):
                continue
            ops_raw = entry.get("ops")
            ops = float(ops_raw) if isinstance(ops_raw, (int, float)) else None
            mem_raw = entry.get("memory_mb")
            memory_mb = float(mem_raw) if isinstance(mem_raw, (int, float)) else None
            metrics[name] = BenchmarkMetrics(
                mean_s=float(mean_s_raw),
                ops=ops,
                memory_mb=memory_mb,
            )
        return metrics
