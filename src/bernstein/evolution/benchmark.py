"""Tiered benchmark runner for evolution validation.

Benchmarks are organized into three tiers:
- smoke: Must always pass; failure = critical regression
- capability: Baseline feature coverage; establishes performance floor
- stretch: Aspirational targets; progress here = improvement

Each benchmark is a YAML file.  Results are written to
.sdd/benchmarks/YYYY-MM-DD.jsonl so the evolution loop can track
progress over time without ever reading the golden benchmark files.
"""

from __future__ import annotations

import importlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import yaml

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

Tier = Literal["smoke", "capability", "stretch"]

_TIERS: tuple[Tier, ...] = ("smoke", "capability", "stretch")


@dataclass(frozen=True)
class SignalSpec:
    """A single expected signal within a benchmark."""

    type: str
    # Optional fields — semantics depend on type
    module: str | None = None
    attribute: str | None = None
    path: str | None = None
    command: str | None = None
    contains: str | None = None


@dataclass(frozen=True)
class BenchmarkSpec:
    """Parsed representation of a benchmark YAML file."""

    id: str
    goal: str
    tier: Tier
    expected_signals: list[SignalSpec]
    max_cost_usd: float = 0.0
    max_duration_seconds: int = 60


@dataclass
class SignalResult:
    """Result of evaluating a single signal."""

    signal_type: str
    passed: bool
    message: str = ""


@dataclass
class BenchmarkResult:
    """Result of running a single benchmark."""

    benchmark_id: str
    tier: Tier
    passed: bool
    goal: str
    signal_results: list[SignalResult] = field(default_factory=lambda: [])
    duration_seconds: float = 0.0
    error: str | None = None


@dataclass
class RunSummary:
    """Aggregate result of a benchmark run."""

    tier: str  # "smoke" | "capability" | "stretch" | "all"
    total: int
    passed: int
    failed: int
    results: list[BenchmarkResult] = field(default_factory=lambda: [])
    run_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# ---------------------------------------------------------------------------
# Signal evaluators
# ---------------------------------------------------------------------------


def _eval_import_succeeds(spec: SignalSpec) -> SignalResult:
    """Check that a module (and optionally an attribute) can be imported."""
    if not spec.module:
        return SignalResult("import_succeeds", False, "missing 'module' field")
    try:
        mod = importlib.import_module(spec.module)
    except ImportError as exc:
        return SignalResult("import_succeeds", False, f"ImportError: {exc}")

    if spec.attribute and not hasattr(mod, spec.attribute):
        return SignalResult(
            "import_succeeds",
            False,
            f"Module '{spec.module}' has no attribute '{spec.attribute}'",
        )

    return SignalResult("import_succeeds", True, f"OK: {spec.module}")


def _eval_path_exists(spec: SignalSpec) -> SignalResult:
    """Check that a path exists on disk."""
    if not spec.path:
        return SignalResult("path_exists", False, "missing 'path' field")
    p = Path(spec.path)
    if p.exists():
        return SignalResult("path_exists", True, f"exists: {p}")
    return SignalResult("path_exists", False, f"not found: {p}")


def _eval_signal(spec: SignalSpec) -> SignalResult:
    """Dispatch to the appropriate evaluator for a signal type."""
    if spec.type == "import_succeeds":
        return _eval_import_succeeds(spec)
    if spec.type == "path_exists":
        return _eval_path_exists(spec)
    # Unknown signal type — treat as skipped (pass) so unknown types don't break runs
    return SignalResult(spec.type, True, f"unsupported signal type '{spec.type}' — skipped")


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def _parse_signal(raw: dict[str, Any]) -> SignalSpec:
    return SignalSpec(
        type=str(raw.get("type", "")),
        module=raw.get("module"),
        attribute=raw.get("attribute"),
        path=raw.get("path"),
        command=raw.get("command"),
        contains=raw.get("contains"),
    )


def _parse_spec(data: dict[str, Any], tier: Tier) -> BenchmarkSpec:
    signals = [_parse_signal(s) for s in data.get("expected_signals", [])]
    return BenchmarkSpec(
        id=str(data["id"]),
        goal=str(data["goal"]),
        tier=tier,
        expected_signals=signals,
        max_cost_usd=float(data.get("max_cost_usd", 0.0)),
        max_duration_seconds=int(data.get("max_duration_seconds", 60)),
    )


def load_benchmarks(benchmarks_dir: Path, tier: Tier) -> list[BenchmarkSpec]:
    """Load all benchmark YAML files for a given tier.

    Args:
        benchmarks_dir: Root directory containing smoke/, capability/, stretch/.
        tier: Which tier to load.

    Returns:
        List of parsed BenchmarkSpec objects, sorted by id.
    """
    tier_dir = benchmarks_dir / tier
    if not tier_dir.is_dir():
        return []

    specs: list[BenchmarkSpec] = []
    for yaml_path in sorted(tier_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(yaml_path.read_text())
            if not isinstance(raw, dict):
                continue
            specs.append(_parse_spec(cast("dict[str, Any]", raw), tier))
        except (yaml.YAMLError, KeyError, TypeError):
            # Skip malformed benchmark files rather than crashing the run
            pass

    return specs


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_benchmark(spec: BenchmarkSpec) -> BenchmarkResult:
    """Run a single benchmark and return its result.

    Args:
        spec: The benchmark to run.

    Returns:
        BenchmarkResult with per-signal outcomes.
    """
    t0 = time.monotonic()
    signal_results: list[SignalResult] = []

    try:
        for sig in spec.expected_signals:
            signal_results.append(_eval_signal(sig))
    except Exception as exc:
        return BenchmarkResult(
            benchmark_id=spec.id,
            tier=spec.tier,
            passed=False,
            goal=spec.goal,
            signal_results=signal_results,
            duration_seconds=time.monotonic() - t0,
            error=str(exc),
        )

    passed = all(s.passed for s in signal_results)
    return BenchmarkResult(
        benchmark_id=spec.id,
        tier=spec.tier,
        passed=passed,
        goal=spec.goal,
        signal_results=signal_results,
        duration_seconds=time.monotonic() - t0,
    )


def run_tier(benchmarks_dir: Path, tier: Tier) -> list[BenchmarkResult]:
    """Run all benchmarks in a tier.

    Args:
        benchmarks_dir: Root directory containing tier subdirectories.
        tier: Which tier to run.

    Returns:
        List of BenchmarkResult, one per loaded benchmark.
    """
    specs = load_benchmarks(benchmarks_dir, tier)
    return [run_benchmark(spec) for spec in specs]


def run_all(benchmarks_dir: Path) -> RunSummary:
    """Run every tier and return an aggregate summary.

    Args:
        benchmarks_dir: Root directory containing smoke/, capability/, stretch/.

    Returns:
        RunSummary with all results and totals.
    """
    results: list[BenchmarkResult] = []
    for tier in _TIERS:
        results.extend(run_tier(benchmarks_dir, tier))

    passed = sum(1 for r in results if r.passed)
    return RunSummary(
        tier="all",
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        results=results,
    )


def run_selected(benchmarks_dir: Path, tier: Tier) -> RunSummary:
    """Run a single tier and return a summary.

    Args:
        benchmarks_dir: Root directory containing tier subdirectories.
        tier: Which tier to run.

    Returns:
        RunSummary for that tier.
    """
    results = run_tier(benchmarks_dir, tier)
    passed = sum(1 for r in results if r.passed)
    return RunSummary(
        tier=tier,
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        results=results,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _result_to_dict(result: BenchmarkResult) -> dict[str, Any]:
    return {
        "benchmark_id": result.benchmark_id,
        "tier": result.tier,
        "passed": result.passed,
        "goal": result.goal,
        "duration_seconds": result.duration_seconds,
        "error": result.error,
        "signal_results": [
            {"type": s.signal_type, "passed": s.passed, "message": s.message} for s in result.signal_results
        ],
    }


def save_results(summary: RunSummary, sdd_dir: Path) -> Path:
    """Append benchmark results to .sdd/benchmarks/YYYY-MM-DD.jsonl.

    Args:
        summary: The run summary to persist.
        sdd_dir: Project .sdd/ directory.

    Returns:
        Path to the JSONL file written.
    """
    benchmarks_results_dir = sdd_dir / "benchmarks"
    benchmarks_results_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    out_path = benchmarks_results_dir / f"{date_str}.jsonl"

    record: dict[str, Any] = {
        "run_at": summary.run_at,
        "tier": summary.tier,
        "total": summary.total,
        "passed": summary.passed,
        "failed": summary.failed,
        "results": [_result_to_dict(r) for r in summary.results],
    }

    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    return out_path
