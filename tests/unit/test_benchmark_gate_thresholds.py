"""Additional unit tests for benchmark gate threshold and baseline promotion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.benchmark_gate import BenchmarkGate, BenchmarkMetrics


def _gate(tmp_path: Path, *, command: str = "benchmark-cmd") -> BenchmarkGate:
    return BenchmarkGate(
        tmp_path,
        tmp_path,
        base_ref="main",
        benchmark_command=command,
    )


def test_benchmark_gate_defaults_to_ten_percent_threshold(tmp_path: Path) -> None:
    gate = _gate(tmp_path)

    assert gate._threshold == pytest.approx(0.10)  # pyright: ignore[reportPrivateUsage]


def test_load_or_measure_baseline_mirrors_legacy_cache_to_canonical(
    tmp_path: Path,
) -> None:
    gate = _gate(tmp_path)
    legacy_path = tmp_path / ".sdd" / "cache" / "benchmark_baseline.json"
    canonical_path = tmp_path / ".sdd" / "metrics" / "benchmark_baseline.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "base_ref": "main",
                "benchmark_command": "benchmark-cmd",
                "metrics": {"bench": {"mean_s": 0.01, "ops": 100.0, "memory_mb": None}},
            }
        ),
        encoding="utf-8",
    )

    baseline = gate._load_or_measure_baseline()  # pyright: ignore[reportPrivateUsage]

    assert baseline["bench"] == BenchmarkMetrics(mean_s=0.01, ops=100.0, memory_mb=None)
    assert canonical_path.exists()
    persisted = json.loads(canonical_path.read_text(encoding="utf-8"))
    assert persisted["metrics"]["bench"]["mean_s"] == pytest.approx(0.01)


def test_promote_candidate_persists_baseline_and_removes_candidate(tmp_path: Path) -> None:
    gate = _gate(tmp_path, command="uv run pytest benchmarks/ --benchmark-json=.benchmark_results.json -q")
    metrics = {"bench": BenchmarkMetrics(mean_s=0.02, ops=80.0, memory_mb=12.5)}

    gate._write_candidate(metrics)  # pyright: ignore[reportPrivateUsage]
    candidate_path = gate._candidate_path()  # pyright: ignore[reportPrivateUsage]

    assert candidate_path.exists()
    assert gate.promote_candidate() is True

    canonical_path = tmp_path / ".sdd" / "metrics" / "benchmark_baseline.json"
    legacy_path = tmp_path / ".sdd" / "cache" / "benchmark_baseline.json"
    assert canonical_path.exists()
    assert legacy_path.exists()
    assert not candidate_path.exists()

    persisted = json.loads(canonical_path.read_text(encoding="utf-8"))
    assert persisted["benchmark_command"] == "uv run pytest benchmarks/ --benchmark-json=.benchmark_results.json -q"
    assert persisted["metrics"]["bench"]["ops"] == pytest.approx(80.0)
