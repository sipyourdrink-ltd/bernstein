"""Unit tests for the performance benchmark gate."""

from __future__ import annotations

import subprocess

import pytest

from bernstein.core.quality.perf_benchmark_gate import (
    BenchmarkGateResult,
    BenchmarkResult,
    BenchmarkSpec,
    RegressionResult,
    compare_benchmarks,
    run_benchmark,
    run_benchmark_gate,
)

# ------------------------------------------------------------------
# BenchmarkResult dataclass
# ------------------------------------------------------------------


def test_benchmark_result_is_frozen() -> None:
    result = BenchmarkResult(name="test", wall_clock_ms=100.0, peak_memory_mb=50.0, throughput=10.0, iterations=3)
    with pytest.raises(AttributeError):
        result.wall_clock_ms = 200.0  # type: ignore[misc]


def test_benchmark_result_optional_fields() -> None:
    result = BenchmarkResult(name="minimal", wall_clock_ms=50.0, peak_memory_mb=None, throughput=None, iterations=1)
    assert result.peak_memory_mb is None
    assert result.throughput is None


# ------------------------------------------------------------------
# RegressionResult dataclass
# ------------------------------------------------------------------


def test_regression_result_is_frozen() -> None:
    before = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=None, throughput=None, iterations=1)
    after = BenchmarkResult(name="t", wall_clock_ms=120.0, peak_memory_mb=None, throughput=None, iterations=1)
    reg = RegressionResult(
        benchmark_name="t",
        before=before,
        after=after,
        wall_clock_delta_pct=20.0,
        memory_delta_pct=None,
        regressed=True,
        threshold_pct=10.0,
    )
    with pytest.raises(AttributeError):
        reg.regressed = False  # type: ignore[misc]


# ------------------------------------------------------------------
# run_benchmark() with mocked subprocess
# ------------------------------------------------------------------


def test_run_benchmark_collects_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "bernstein.core.quality.perf_benchmark_gate._get_peak_memory_mb",
        lambda: None,
    )

    result = run_benchmark("echo hello", name="echo_test", iterations=3)

    assert result.name == "echo_test"
    assert result.wall_clock_ms > 0
    assert result.iterations == 3
    assert result.peak_memory_mb is None
    assert call_count == 3


def test_run_benchmark_raises_on_command_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="bad")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="failed on iteration"):
        run_benchmark("failing-cmd", name="fail_test", iterations=1)


def test_run_benchmark_raises_on_zero_iterations() -> None:
    with pytest.raises(ValueError, match="iterations must be >= 1"):
        run_benchmark("echo ok", iterations=0)


def test_run_benchmark_defaults_name_to_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "bernstein.core.quality.perf_benchmark_gate._get_peak_memory_mb",
        lambda: None,
    )

    result = run_benchmark("echo hello", iterations=1)
    assert result.name == "echo hello"


def test_run_benchmark_captures_peak_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "bernstein.core.quality.perf_benchmark_gate._get_peak_memory_mb",
        lambda: 42.5,
    )

    result = run_benchmark("echo mem", name="mem_test", iterations=2)
    assert result.peak_memory_mb == 42.5


def test_run_benchmark_computes_throughput(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "bernstein.core.quality.perf_benchmark_gate._get_peak_memory_mb",
        lambda: None,
    )

    result = run_benchmark("echo throughput", name="tp_test", iterations=1)
    # throughput = 1000 / wall_clock_ms, should be positive
    assert result.throughput is not None
    assert result.throughput > 0


# ------------------------------------------------------------------
# compare_benchmarks()
# ------------------------------------------------------------------


def test_compare_no_regression() -> None:
    before = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=50.0, throughput=10.0, iterations=3)
    after = BenchmarkResult(name="t", wall_clock_ms=105.0, peak_memory_mb=52.0, throughput=9.5, iterations=3)

    result = compare_benchmarks(before, after, threshold=10.0)

    assert not result.regressed
    assert result.wall_clock_delta_pct == pytest.approx(5.0, abs=0.1)
    assert result.memory_delta_pct == pytest.approx(4.0, abs=0.1)
    assert result.threshold_pct == 10.0


def test_compare_wall_clock_regression() -> None:
    before = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=None, throughput=None, iterations=3)
    after = BenchmarkResult(name="t", wall_clock_ms=115.0, peak_memory_mb=None, throughput=None, iterations=3)

    result = compare_benchmarks(before, after, threshold=10.0)

    assert result.regressed
    assert result.wall_clock_delta_pct == pytest.approx(15.0, abs=0.1)
    assert result.memory_delta_pct is None


def test_compare_memory_regression() -> None:
    before = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=50.0, throughput=None, iterations=3)
    after = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=60.0, throughput=None, iterations=3)

    result = compare_benchmarks(before, after, threshold=10.0)

    assert result.regressed
    assert result.wall_clock_delta_pct == pytest.approx(0.0, abs=0.1)
    assert result.memory_delta_pct == pytest.approx(20.0, abs=0.1)


def test_compare_exactly_at_threshold_does_not_regress() -> None:
    before = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=None, throughput=None, iterations=1)
    after = BenchmarkResult(name="t", wall_clock_ms=110.0, peak_memory_mb=None, throughput=None, iterations=1)

    result = compare_benchmarks(before, after, threshold=10.0)

    assert not result.regressed
    assert result.wall_clock_delta_pct == pytest.approx(10.0, abs=0.1)


def test_compare_improvement_is_not_regression() -> None:
    before = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=50.0, throughput=None, iterations=1)
    after = BenchmarkResult(name="t", wall_clock_ms=80.0, peak_memory_mb=40.0, throughput=None, iterations=1)

    result = compare_benchmarks(before, after, threshold=10.0)

    assert not result.regressed
    assert result.wall_clock_delta_pct < 0
    assert result.memory_delta_pct is not None
    assert result.memory_delta_pct < 0


def test_compare_zero_baseline_wall_clock() -> None:
    before = BenchmarkResult(name="t", wall_clock_ms=0.0, peak_memory_mb=None, throughput=None, iterations=1)
    after = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=None, throughput=None, iterations=1)

    result = compare_benchmarks(before, after, threshold=10.0)

    assert result.wall_clock_delta_pct == 0.0
    assert not result.regressed


def test_compare_zero_baseline_memory() -> None:
    before = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=0.0, throughput=None, iterations=1)
    after = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=50.0, throughput=None, iterations=1)

    result = compare_benchmarks(before, after, threshold=10.0)

    # memory_delta_pct should be None when baseline is zero
    assert result.memory_delta_pct is None
    assert not result.regressed


def test_compare_custom_threshold() -> None:
    before = BenchmarkResult(name="t", wall_clock_ms=100.0, peak_memory_mb=None, throughput=None, iterations=1)
    after = BenchmarkResult(name="t", wall_clock_ms=125.0, peak_memory_mb=None, throughput=None, iterations=1)

    # 25% increase, 30% threshold => no regression
    no_reg = compare_benchmarks(before, after, threshold=30.0)
    assert not no_reg.regressed

    # 25% increase, 20% threshold => regression
    reg = compare_benchmarks(before, after, threshold=20.0)
    assert reg.regressed


# ------------------------------------------------------------------
# run_benchmark_gate()
# ------------------------------------------------------------------


def test_gate_passes_when_no_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    before = BenchmarkResult(name="b1", wall_clock_ms=100.0, peak_memory_mb=50.0, throughput=10.0, iterations=3)
    after = BenchmarkResult(name="b1", wall_clock_ms=105.0, peak_memory_mb=52.0, throughput=9.5, iterations=3)

    monkeypatch.setattr(
        "bernstein.core.quality.perf_benchmark_gate.run_benchmark",
        lambda command, *, name=None, iterations=3: after,
    )

    specs = [BenchmarkSpec(command="echo ok", name="b1", iterations=3)]
    result = run_benchmark_gate(specs, before_results=[before], threshold=10.0)

    assert result.passed
    assert len(result.results) == 1
    assert not result.results[0].regressed
    assert "within" in result.summary


def test_gate_fails_on_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    before = BenchmarkResult(name="slow", wall_clock_ms=100.0, peak_memory_mb=None, throughput=None, iterations=3)
    after = BenchmarkResult(name="slow", wall_clock_ms=200.0, peak_memory_mb=None, throughput=None, iterations=3)

    monkeypatch.setattr(
        "bernstein.core.quality.perf_benchmark_gate.run_benchmark",
        lambda command, *, name=None, iterations=3: after,
    )

    specs = [BenchmarkSpec(command="echo slow", name="slow", iterations=3)]
    result = run_benchmark_gate(specs, before_results=[before], threshold=10.0)

    assert not result.passed
    assert len(result.results) == 1
    assert result.results[0].regressed
    assert "regression detected" in result.summary.lower()


def test_gate_empty_benchmarks() -> None:
    result = run_benchmark_gate([], threshold=10.0)

    assert result.passed
    assert result.results == []
    assert "No benchmarks" in result.summary


def test_gate_multiple_benchmarks_partial_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    before_fast = BenchmarkResult(name="fast", wall_clock_ms=100.0, peak_memory_mb=None, throughput=None, iterations=3)
    before_slow = BenchmarkResult(name="slow", wall_clock_ms=100.0, peak_memory_mb=None, throughput=None, iterations=3)

    results_map = {
        "fast": BenchmarkResult(name="fast", wall_clock_ms=105.0, peak_memory_mb=None, throughput=None, iterations=3),
        "slow": BenchmarkResult(name="slow", wall_clock_ms=150.0, peak_memory_mb=None, throughput=None, iterations=3),
    }

    def fake_run(command: str, *, name: str | None = None, iterations: int = 3) -> BenchmarkResult:
        return results_map[name or command]

    monkeypatch.setattr(
        "bernstein.core.quality.perf_benchmark_gate.run_benchmark",
        fake_run,
    )

    specs = [
        BenchmarkSpec(command="echo fast", name="fast"),
        BenchmarkSpec(command="echo slow", name="slow"),
    ]
    result = run_benchmark_gate(
        specs,
        before_results=[before_fast, before_slow],
        threshold=10.0,
    )

    assert not result.passed
    assert len(result.results) == 2
    regressions = [r for r in result.results if r.regressed]
    assert len(regressions) == 1
    assert regressions[0].benchmark_name == "slow"


def test_gate_runs_before_when_no_baseline_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def fake_run(command: str, *, name: str | None = None, iterations: int = 3) -> BenchmarkResult:
        nonlocal call_count
        call_count += 1
        return BenchmarkResult(
            name=name or command,
            wall_clock_ms=100.0,
            peak_memory_mb=None,
            throughput=10.0,
            iterations=iterations,
        )

    monkeypatch.setattr(
        "bernstein.core.quality.perf_benchmark_gate.run_benchmark",
        fake_run,
    )

    specs = [BenchmarkSpec(command="echo test", name="auto")]
    result = run_benchmark_gate(specs, threshold=10.0)

    # Should have called run_benchmark twice: once for before, once for after
    assert call_count == 2
    assert result.passed


def test_gate_result_is_frozen() -> None:
    gate_result = BenchmarkGateResult(passed=True, results=[], summary="ok")
    with pytest.raises(AttributeError):
        gate_result.passed = False  # type: ignore[misc]


def test_gate_summary_includes_memory_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    before = BenchmarkResult(name="mem", wall_clock_ms=100.0, peak_memory_mb=50.0, throughput=None, iterations=3)
    after = BenchmarkResult(name="mem", wall_clock_ms=100.0, peak_memory_mb=70.0, throughput=None, iterations=3)

    monkeypatch.setattr(
        "bernstein.core.quality.perf_benchmark_gate.run_benchmark",
        lambda command, *, name=None, iterations=3: after,
    )

    specs = [BenchmarkSpec(command="echo mem", name="mem", iterations=3)]
    result = run_benchmark_gate(specs, before_results=[before], threshold=10.0)

    assert not result.passed
    assert "memory" in result.summary


# ------------------------------------------------------------------
# BenchmarkSpec dataclass
# ------------------------------------------------------------------


def test_benchmark_spec_defaults() -> None:
    spec = BenchmarkSpec(command="echo test", name="test_spec")
    assert spec.iterations == 3


def test_benchmark_spec_is_frozen() -> None:
    spec = BenchmarkSpec(command="echo test", name="test_spec")
    with pytest.raises(AttributeError):
        spec.command = "echo other"  # type: ignore[misc]
