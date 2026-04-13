"""Benchmark regression gate: block merge when performance degrades beyond threshold."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bernstein.core.git_basic import run_git

logger = logging.getLogger(__name__)


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_OBJ = "dict[str, object]"


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
    CANONICAL_BASELINE_FILE = Path(".sdd/metrics/benchmark_baseline.json")
    CANDIDATE_DIR = Path(".sdd/runtime/benchmark_candidates")
    RESULTS_FILE = ".benchmark_results.json"
    DEFAULT_COMMAND = "uv run pytest benchmarks/ --benchmark-json=.benchmark_results.json -q"

    def __init__(
        self,
        workdir: Path,
        run_dir: Path,
        *,
        base_ref: str = "main",
        benchmark_command: str | None = None,
        threshold: float = 0.10,
    ) -> None:
        self._workdir = workdir
        self._run_dir = run_dir
        self._base_ref = base_ref
        self._benchmark_command = benchmark_command or self.DEFAULT_COMMAND
        self._threshold = threshold
        self._baseline_path = workdir / self.CANONICAL_BASELINE_FILE
        self._legacy_baseline_path = workdir / self.BASELINE_FILE

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
        if passed:
            self._write_candidate(current)
        detail = self._format_detail(baseline, current, regressions)
        return BenchmarkEvaluation(
            passed=passed,
            regressions=regressions,
            detail=detail,
            baseline_metrics=baseline,
            current_metrics=current,
        )

    def promote_candidate(self) -> bool:
        """Promote the current successful benchmark candidate into the baseline cache."""
        candidate_path = self._candidate_path()
        payload = self._load_cached_baseline(candidate_path)
        if payload is None:
            return False
        self._persist_baseline_payload(payload)
        with contextlib.suppress(OSError):
            candidate_path.unlink()
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_or_measure_baseline(self) -> dict[str, BenchmarkMetrics]:
        """Load the cached baseline when compatible, else re-measure it."""
        canonical = self._load_cached_baseline(self._baseline_path)
        if canonical is not None:
            return self._deserialize_metrics(cast(_CAST_DICT_STR_OBJ, canonical["metrics"]))

        legacy = self._load_cached_baseline(self._legacy_baseline_path)
        if legacy is not None:
            self._persist_baseline_payload(legacy)
            return self._deserialize_metrics(cast(_CAST_DICT_STR_OBJ, legacy["metrics"]))

        baseline = self.measure_baseline()
        self._persist_baseline(baseline)
        return baseline

    def _run_measurement(self, cwd: Path) -> dict[str, BenchmarkMetrics]:
        """Execute the benchmark command in ``cwd`` and parse results."""
        result = subprocess.run(
            shlex.split(self._benchmark_command),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
        return self._extract_metrics(cast(_CAST_DICT_STR_OBJ, raw))

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
            bench_map = cast(_CAST_DICT_STR_OBJ, bench_item)
            name = bench_map.get("name")
            if not isinstance(name, str):
                continue
            stats_raw = bench_map.get("stats")
            if not isinstance(stats_raw, dict):
                continue
            stats = cast(_CAST_DICT_STR_OBJ, stats_raw)
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

    def _check_metric_regression(
        self,
        name: str,
        metric: str,
        baseline_val: float | None,
        current_val: float | None,
        *,
        higher_is_worse: bool,
    ) -> BenchmarkRegression | None:
        """Check a single metric for regression exceeding the threshold."""
        if baseline_val is None or current_val is None or baseline_val <= 0:
            return None
        if higher_is_worse:
            delta = (current_val - baseline_val) / baseline_val
        else:
            delta = (baseline_val - current_val) / baseline_val
        if delta <= self._threshold:
            return None
        return BenchmarkRegression(
            name=name,
            metric=metric,
            baseline=baseline_val,
            current=current_val,
            delta_pct=round(delta * 100, 2),
        )

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
            for metric, base_val, curr_val, higher_is_worse in (
                ("mean_s", base.mean_s, curr.mean_s, True),
                ("ops", base.ops, curr.ops, False),
                ("memory_mb", base.memory_mb, curr.memory_mb, True),
            ):
                reg = self._check_metric_regression(
                    name, metric, base_val, curr_val, higher_is_worse=higher_is_worse,
                )
                if reg is not None:
                    regressions.append(reg)
        return regressions

    def _format_detail(
        self,
        _baseline: dict[str, BenchmarkMetrics],
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
            entry = cast(_CAST_DICT_STR_OBJ, raw)
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

    def _load_cached_baseline(self, path: Path) -> dict[str, object] | None:
        """Load a cached baseline payload when it matches the active config."""
        if not path.exists():
            return None
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        payload = cast(_CAST_DICT_STR_OBJ, raw)
        if payload.get("base_ref") != self._base_ref or payload.get("benchmark_command") != self._benchmark_command:
            return None
        metrics_raw = payload.get("metrics")
        if not isinstance(metrics_raw, dict):
            return None
        try:
            self._deserialize_metrics(cast(_CAST_DICT_STR_OBJ, metrics_raw))
        except (KeyError, TypeError, ValueError):
            return None
        return payload

    def _persist_baseline(self, metrics: dict[str, BenchmarkMetrics]) -> None:
        """Persist baseline metrics to canonical and legacy cache paths."""
        self._persist_baseline_payload(self._build_payload(metrics))

    def _persist_baseline_payload(self, payload: dict[str, object]) -> None:
        """Persist a validated baseline payload to both cache locations."""
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        for path in (self._baseline_path, self._legacy_baseline_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(serialized, encoding="utf-8")

    def _write_candidate(self, metrics: dict[str, BenchmarkMetrics]) -> None:
        """Write a successful current measurement for promotion after merge."""
        candidate_path = self._candidate_path()
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(
            json.dumps(self._build_payload(metrics), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _candidate_path(self) -> Path:
        """Return the candidate baseline path for the active benchmark command."""
        digest = hashlib.sha256(self._benchmark_command.encode("utf-8")).hexdigest()[:12]
        return self._workdir / self.CANDIDATE_DIR / f"{self._base_ref}-{digest}.json"

    def _build_payload(self, metrics: dict[str, BenchmarkMetrics]) -> dict[str, object]:
        """Build the persisted JSON payload for baseline or candidate metrics."""
        return {
            "base_ref": self._base_ref,
            "benchmark_command": self._benchmark_command,
            "metrics": self._serialize_metrics(metrics),
        }
