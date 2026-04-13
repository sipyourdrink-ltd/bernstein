"""Unit tests for the benchmark regression gate."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.benchmark_gate import (
    BenchmarkGate,
    BenchmarkMetrics,
)


def _gate(
    tmp_path: Path,
    *,
    base_ref: str = "main",
    command: str = "benchmark-cmd",
    threshold: float = 0.15,
) -> BenchmarkGate:
    return BenchmarkGate(
        tmp_path,
        tmp_path,
        base_ref=base_ref,
        benchmark_command=command,
        threshold=threshold,
    )


def _write_results(path: Path, benchmarks: list[dict[str, object]]) -> None:
    results_path = path / ".benchmark_results.json"
    results_path.write_text(json.dumps({"benchmarks": benchmarks}), encoding="utf-8")


# ---------------------------------------------------------------------------
# evaluate() — pass / fail
# ---------------------------------------------------------------------------


def test_evaluate_passes_when_no_regression(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    baseline = {"test_foo": BenchmarkMetrics(mean_s=0.010, ops=100.0, memory_mb=None)}
    current = {"test_foo": BenchmarkMetrics(mean_s=0.011, ops=95.0, memory_mb=None)}
    # 10% slower and 5% less throughput — both under 15% threshold
    monkeypatch.setattr(gate, "_load_or_measure_baseline", lambda: baseline)
    monkeypatch.setattr(gate, "measure_current", lambda: current)

    result = gate.evaluate()

    assert result.passed is True
    assert result.regressions == []
    assert "within" in result.detail


def test_evaluate_fails_on_response_time_regression(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path, threshold=0.15)
    baseline = {"test_bar": BenchmarkMetrics(mean_s=0.010, ops=None, memory_mb=None)}
    current = {"test_bar": BenchmarkMetrics(mean_s=0.020, ops=None, memory_mb=None)}
    # 100% slower → regression
    monkeypatch.setattr(gate, "_load_or_measure_baseline", lambda: baseline)
    monkeypatch.setattr(gate, "measure_current", lambda: current)

    result = gate.evaluate()

    assert result.passed is False
    assert len(result.regressions) == 1
    reg = result.regressions[0]
    assert reg.name == "test_bar"
    assert reg.metric == "mean_s"
    assert reg.delta_pct == pytest.approx(100.0, abs=0.1)
    assert "response time" in result.detail


def test_evaluate_fails_on_throughput_regression(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path, threshold=0.15)
    baseline = {"test_baz": BenchmarkMetrics(mean_s=0.010, ops=1000.0, memory_mb=None)}
    current = {"test_baz": BenchmarkMetrics(mean_s=0.010, ops=500.0, memory_mb=None)}
    # 50% throughput drop → regression
    monkeypatch.setattr(gate, "_load_or_measure_baseline", lambda: baseline)
    monkeypatch.setattr(gate, "measure_current", lambda: current)

    result = gate.evaluate()

    assert result.passed is False
    regressions = [r for r in result.regressions if r.metric == "ops"]
    assert len(regressions) == 1
    assert regressions[0].delta_pct == pytest.approx(50.0, abs=0.1)
    assert "throughput" in result.detail


def test_evaluate_fails_on_memory_regression(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path, threshold=0.15)
    baseline = {"test_mem": BenchmarkMetrics(mean_s=0.001, ops=None, memory_mb=50.0)}
    current = {"test_mem": BenchmarkMetrics(mean_s=0.001, ops=None, memory_mb=80.0)}
    # 60% memory increase → regression
    monkeypatch.setattr(gate, "_load_or_measure_baseline", lambda: baseline)
    monkeypatch.setattr(gate, "measure_current", lambda: current)

    result = gate.evaluate()

    assert result.passed is False
    regressions = [r for r in result.regressions if r.metric == "memory_mb"]
    assert len(regressions) == 1
    assert "memory" in result.detail


def test_evaluate_ignores_new_benchmarks_without_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    baseline: dict[str, BenchmarkMetrics] = {}
    current = {"test_new": BenchmarkMetrics(mean_s=0.001, ops=None, memory_mb=None)}
    monkeypatch.setattr(gate, "_load_or_measure_baseline", lambda: baseline)
    monkeypatch.setattr(gate, "measure_current", lambda: current)

    result = gate.evaluate()

    assert result.passed is True
    assert result.regressions == []


# ---------------------------------------------------------------------------
# _detect_regressions()
# ---------------------------------------------------------------------------


def test_detect_regressions_below_threshold_is_clean(tmp_path: Path) -> None:
    gate = _gate(tmp_path, threshold=0.20)
    baseline = {"t": BenchmarkMetrics(mean_s=1.0, ops=100.0, memory_mb=100.0)}
    current = {"t": BenchmarkMetrics(mean_s=1.15, ops=85.0, memory_mb=115.0)}
    # mean_s +15%, ops -15%, memory +15% — all at threshold or below
    regressions = gate._detect_regressions(baseline, current)
    assert regressions == []


def test_detect_regressions_exactly_at_threshold_is_clean(tmp_path: Path) -> None:
    gate = _gate(tmp_path, threshold=0.15)
    baseline = {"t": BenchmarkMetrics(mean_s=1.0, ops=None, memory_mb=None)}
    # Exactly 15% slower — must NOT block (> threshold, not >=)
    current = {"t": BenchmarkMetrics(mean_s=1.15, ops=None, memory_mb=None)}
    regressions = gate._detect_regressions(baseline, current)
    assert regressions == []


def test_detect_regressions_above_threshold_triggers(tmp_path: Path) -> None:
    gate = _gate(tmp_path, threshold=0.15)
    baseline = {"t": BenchmarkMetrics(mean_s=1.0, ops=None, memory_mb=None)}
    current = {"t": BenchmarkMetrics(mean_s=1.16, ops=None, memory_mb=None)}
    regressions = gate._detect_regressions(baseline, current)
    assert len(regressions) == 1
    assert regressions[0].metric == "mean_s"


# ---------------------------------------------------------------------------
# _parse_results() / _extract_metrics()
# ---------------------------------------------------------------------------


def test_parse_results_reads_pytest_benchmark_json(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    _write_results(
        tmp_path,
        [
            {"name": "test_a", "stats": {"mean": 0.001, "ops": 1000.0}},
            {"name": "test_b", "stats": {"mean": 0.002}},
        ],
    )

    metrics = gate._parse_results(tmp_path)

    assert "test_a" in metrics
    assert metrics["test_a"].mean_s == pytest.approx(0.001)
    assert metrics["test_a"].ops == pytest.approx(1000.0)
    assert "test_b" in metrics
    assert metrics["test_b"].ops is None


def test_parse_results_includes_memory_mb_when_present(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    _write_results(
        tmp_path,
        [{"name": "test_mem", "stats": {"mean": 0.005, "memory_mb": 42.5}}],
    )

    metrics = gate._parse_results(tmp_path)

    assert metrics["test_mem"].memory_mb == pytest.approx(42.5)


def test_parse_results_raises_when_file_missing(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    with pytest.raises(RuntimeError, match="not found"):
        gate._parse_results(tmp_path)


def test_parse_results_raises_on_empty_benchmarks(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    _write_results(tmp_path, [])
    with pytest.raises(RuntimeError, match="No benchmarks found"):
        gate._parse_results(tmp_path)


def test_parse_results_raises_on_missing_benchmarks_key(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    results_path = tmp_path / ".benchmark_results.json"
    results_path.write_text(json.dumps({"other": []}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing 'benchmarks'"):
        gate._parse_results(tmp_path)


# ---------------------------------------------------------------------------
# Baseline caching
# ---------------------------------------------------------------------------


def test_load_or_measure_baseline_uses_valid_cache(tmp_path: Path) -> None:
    gate = _gate(tmp_path, base_ref="main", command="cmd-a")
    baseline_path = tmp_path / ".sdd" / "cache" / "benchmark_baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "base_ref": "main",
                "benchmark_command": "cmd-a",
                "metrics": {"test_x": {"mean_s": 0.005, "ops": None, "memory_mb": None}},
            }
        ),
        encoding="utf-8",
    )

    baseline = gate._load_or_measure_baseline()

    assert "test_x" in baseline
    assert baseline["test_x"].mean_s == pytest.approx(0.005)


def test_load_or_measure_baseline_remeasures_on_ref_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path, base_ref="release", command="cmd-b")
    baseline_path = tmp_path / ".sdd" / "cache" / "benchmark_baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "base_ref": "main",
                "benchmark_command": "cmd-a",
                "metrics": {"old": {"mean_s": 99.9, "ops": None, "memory_mb": None}},
            }
        ),
        encoding="utf-8",
    )
    fresh: dict[str, BenchmarkMetrics] = {"new": BenchmarkMetrics(mean_s=0.001, ops=None, memory_mb=None)}
    monkeypatch.setattr(gate, "measure_baseline", lambda: fresh)

    baseline = gate._load_or_measure_baseline()
    persisted = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert "new" in baseline
    assert persisted["base_ref"] == "release"
    assert persisted["benchmark_command"] == "cmd-b"


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


def test_serialize_deserialize_round_trip(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    original: dict[str, BenchmarkMetrics] = {
        "t1": BenchmarkMetrics(mean_s=0.01, ops=100.0, memory_mb=None),
        "t2": BenchmarkMetrics(mean_s=0.02, ops=None, memory_mb=55.5),
    }

    serialized = gate._serialize_metrics(original)
    restored = gate._deserialize_metrics(serialized)

    assert set(restored.keys()) == {"t1", "t2"}
    assert restored["t1"].mean_s == pytest.approx(0.01)
    assert restored["t1"].ops == pytest.approx(100.0)
    assert restored["t1"].memory_mb is None
    assert restored["t2"].memory_mb == pytest.approx(55.5)


# ---------------------------------------------------------------------------
# _format_detail()
# ---------------------------------------------------------------------------


def test_format_detail_all_passing(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    baseline: dict[str, BenchmarkMetrics] = {}
    current = {"t": BenchmarkMetrics(mean_s=0.001, ops=None, memory_mb=None)}
    detail = gate._format_detail(baseline, current, [])
    assert "within" in detail
    assert "1 benchmark" in detail


def test_format_detail_with_regressions(tmp_path: Path) -> None:
    gate = _gate(tmp_path, threshold=0.15)
    baseline = {"t": BenchmarkMetrics(mean_s=0.01, ops=100.0, memory_mb=50.0)}
    current = {"t": BenchmarkMetrics(mean_s=0.02, ops=50.0, memory_mb=100.0)}
    regressions = gate._detect_regressions(baseline, current)
    detail = gate._format_detail(baseline, current, regressions)
    assert "regression detected" in detail.lower()
    assert "response time" in detail
    assert "throughput" in detail
    assert "memory" in detail
