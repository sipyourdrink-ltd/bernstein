"""Unit tests for bernstein.benchmark.reproducible."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.benchmark.comparative import BenchmarkTask
from bernstein.benchmark.reproducible import (
    COST_REGRESSION_THRESHOLD,
    QUALITY_REGRESSION_THRESHOLD_PP,
    THROUGHPUT_REGRESSION_THRESHOLD,
    BenchmarkConfig,
    BenchmarkRun,
    CostMetrics,
    QualityMetrics,
    RegressionReport,
    ReproducibleBenchmark,
    TaskRunRecord,
    ThroughputMetrics,
    _build_cost,
    _build_quality,
    _build_throughput,
    _derive_run_id,
    _simulate_task,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_task(task_id: str = "t001", task_type: str = "bugfix", files: list[str] | None = None) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        description="Fix the thing",
        task_type=task_type,  # type: ignore[arg-type]
        files=files or ["src/foo.py"],
        expected_outcome="Tests pass",
    )


def _make_record(
    task_id: str = "t001",
    elapsed_s: float = 15.0,
    cost_usd: float = 0.001,
    tokens: int = 500,
    passed: bool = True,
    verified: bool = True,
) -> TaskRunRecord:
    return TaskRunRecord(
        task_id=task_id,
        elapsed_s=elapsed_s,
        cost_usd=cost_usd,
        tokens=tokens,
        passed=passed,
        verified=verified,
    )


def _make_run(
    run_id: str = "abc123",
    tasks_per_hour: float = 100.0,
    per_task_usd: float = 0.001,
    pass_rate: float = 0.85,
    task_count: int = 10,
) -> BenchmarkRun:
    return BenchmarkRun(
        run_id=run_id,
        timestamp="2026-01-01T00:00:00+00:00",
        seed=42,
        task_count=task_count,
        throughput=ThroughputMetrics(
            tasks_completed=task_count,
            total_elapsed_s=3600.0 / tasks_per_hour * task_count,
            tasks_per_hour=tasks_per_hour,
            p50_latency_s=36.0,
            p95_latency_s=60.0,
        ),
        cost=CostMetrics(
            total_usd=per_task_usd * task_count,
            per_task_usd=per_task_usd,
            total_tokens=task_count * 500,
        ),
        quality=QualityMetrics(
            pass_rate=pass_rate,
            verification_rate=pass_rate * 0.95,
            total_tasks=task_count,
            passed=int(pass_rate * task_count),
        ),
    )


# ---------------------------------------------------------------------------
# _derive_run_id
# ---------------------------------------------------------------------------


def test_derive_run_id_is_12_hex_chars() -> None:
    rid = _derive_run_id(42, ["t1", "t2"])
    assert len(rid) == 12
    assert all(c in "0123456789abcdef" for c in rid)


def test_derive_run_id_is_deterministic() -> None:
    a = _derive_run_id(42, ["t1", "t2"])
    b = _derive_run_id(42, ["t1", "t2"])
    assert a == b


def test_derive_run_id_changes_with_seed() -> None:
    a = _derive_run_id(42, ["t1"])
    b = _derive_run_id(99, ["t1"])
    assert a != b


def test_derive_run_id_independent_of_task_order() -> None:
    # Sorted internally, so order shouldn't matter
    a = _derive_run_id(42, ["t1", "t2"])
    b = _derive_run_id(42, ["t2", "t1"])
    assert a == b


def test_derive_run_id_changes_with_different_tasks() -> None:
    a = _derive_run_id(42, ["t1", "t2"])
    b = _derive_run_id(42, ["t1", "t3"])
    assert a != b


# ---------------------------------------------------------------------------
# _simulate_task
# ---------------------------------------------------------------------------


import random


def test_simulate_task_returns_task_run_record() -> None:
    import random

    task = _make_task()
    rng = random.Random(42)
    record = _simulate_task(task, rng)
    assert isinstance(record, TaskRunRecord)
    assert record.task_id == "t001"


def test_simulate_task_elapsed_is_positive() -> None:
    task = _make_task(task_type="bugfix")
    rng = random.Random(42)
    record = _simulate_task(task, rng)
    assert record.elapsed_s > 0


def test_simulate_task_cost_is_positive() -> None:
    task = _make_task(task_type="docs")
    rng = random.Random(42)
    record = _simulate_task(task, rng)
    assert record.cost_usd > 0


def test_simulate_task_tokens_positive() -> None:
    task = _make_task()
    rng = random.Random(42)
    record = _simulate_task(task, rng)
    assert record.tokens >= 100


def test_simulate_task_is_deterministic() -> None:
    task = _make_task()
    a = _simulate_task(task, random.Random(7))
    b = _simulate_task(task, random.Random(7))
    assert a.elapsed_s == b.elapsed_s
    assert a.cost_usd == b.cost_usd
    assert a.tokens == b.tokens
    assert a.passed == b.passed


def test_simulate_task_all_type_variants() -> None:
    for task_type in ("bugfix", "test", "refactor", "docs", "unknown"):
        task = _make_task(task_type=task_type)
        record = _simulate_task(task, random.Random(42))
        assert record.elapsed_s > 0


def test_simulate_task_verified_only_when_passed() -> None:
    # With many iterations, verified should never be True when passed is False
    task = _make_task(task_type="bugfix")
    for seed in range(50):
        record = _simulate_task(task, random.Random(seed))
        if not record.passed:
            assert not record.verified


# ---------------------------------------------------------------------------
# _build_throughput
# ---------------------------------------------------------------------------


def test_build_throughput_empty_records() -> None:
    m = _build_throughput([])
    assert m.tasks_completed == 0
    assert m.tasks_per_hour == pytest.approx(0.0)
    assert m.p50_latency_s == pytest.approx(0.0)
    assert m.p95_latency_s == pytest.approx(0.0)


def test_build_throughput_single_record() -> None:
    records = [_make_record(elapsed_s=3600.0)]
    m = _build_throughput(records)
    assert m.tasks_completed == 1
    assert abs(m.tasks_per_hour - 1.0) < 0.01


def test_build_throughput_computes_p95() -> None:
    records = [_make_record(elapsed_s=float(i)) for i in range(1, 21)]
    m = _build_throughput(records)
    # p95 of 20 records: idx = min(int(20*0.95), 19) = min(19, 19) = 19 → latencies[19] = 20
    assert m.p95_latency_s == pytest.approx(20.0)


def test_build_throughput_p50_is_median() -> None:
    records = [_make_record(elapsed_s=float(i)) for i in [10, 20, 30]]
    m = _build_throughput(records)
    assert m.p50_latency_s == pytest.approx(20.0)


def test_build_throughput_serializes_to_dict() -> None:
    records = [_make_record(elapsed_s=60.0)]
    m = _build_throughput(records)
    d = m.to_dict()
    assert set(d.keys()) == {"tasks_completed", "total_elapsed_s", "tasks_per_hour", "p50_latency_s", "p95_latency_s"}


def test_build_throughput_roundtrip() -> None:
    records = [_make_record(elapsed_s=60.0)]
    m = _build_throughput(records)
    restored = ThroughputMetrics.from_dict(m.to_dict())
    assert restored.tasks_completed == m.tasks_completed
    assert abs(restored.tasks_per_hour - m.tasks_per_hour) < 0.01


# ---------------------------------------------------------------------------
# _build_cost
# ---------------------------------------------------------------------------


def test_build_cost_empty_records() -> None:
    m = _build_cost([])
    assert m.total_usd == pytest.approx(0.0)
    assert m.per_task_usd == pytest.approx(0.0)
    assert m.total_tokens == 0


def test_build_cost_sums_correctly() -> None:
    records = [
        _make_record(cost_usd=0.001, tokens=400),
        _make_record(cost_usd=0.003, tokens=600),
    ]
    m = _build_cost(records)
    assert abs(m.total_usd - 0.004) < 1e-9
    assert abs(m.per_task_usd - 0.002) < 1e-9
    assert m.total_tokens == 1000


def test_build_cost_roundtrip() -> None:
    records = [_make_record(cost_usd=0.002, tokens=800)]
    m = _build_cost(records)
    restored = CostMetrics.from_dict(m.to_dict())
    assert abs(restored.total_usd - m.total_usd) < 1e-9
    assert restored.total_tokens == m.total_tokens


# ---------------------------------------------------------------------------
# _build_quality
# ---------------------------------------------------------------------------


def test_build_quality_empty_records() -> None:
    m = _build_quality([])
    assert m.pass_rate == pytest.approx(0.0)
    assert m.total_tasks == 0


def test_build_quality_all_pass() -> None:
    records = [_make_record(passed=True, verified=True) for _ in range(5)]
    m = _build_quality(records)
    assert m.pass_rate == pytest.approx(1.0)
    assert m.verification_rate == pytest.approx(1.0)
    assert m.passed == 5


def test_build_quality_none_pass() -> None:
    records = [_make_record(passed=False, verified=False) for _ in range(4)]
    m = _build_quality(records)
    assert m.pass_rate == pytest.approx(0.0)
    assert m.passed == 0


def test_build_quality_partial_pass() -> None:
    records = [
        _make_record(passed=True, verified=True),
        _make_record(passed=True, verified=False),
        _make_record(passed=False, verified=False),
        _make_record(passed=False, verified=False),
    ]
    m = _build_quality(records)
    assert abs(m.pass_rate - 0.5) < 0.001
    assert abs(m.verification_rate - 0.25) < 0.001


def test_build_quality_roundtrip() -> None:
    records = [_make_record(passed=True, verified=True) for _ in range(3)]
    m = _build_quality(records)
    restored = QualityMetrics.from_dict(m.to_dict())
    assert restored.pass_rate == m.pass_rate
    assert restored.total_tasks == m.total_tasks


# ---------------------------------------------------------------------------
# ReproducibleBenchmark.run
# ---------------------------------------------------------------------------


def test_run_produces_benchmark_run() -> None:
    tasks = [_make_task(f"t{i}") for i in range(5)]
    bench = ReproducibleBenchmark(tasks=tasks)
    run = bench.run()
    assert isinstance(run, BenchmarkRun)
    assert run.task_count == 5
    assert len(run.records) == 5


def test_run_is_deterministic() -> None:
    tasks = [_make_task(f"t{i}") for i in range(8)]
    bench = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=42))
    run1 = bench.run()
    run2 = bench.run()
    assert run1.run_id == run2.run_id
    assert run1.quality.pass_rate == run2.quality.pass_rate
    assert run1.throughput.tasks_per_hour == run2.throughput.tasks_per_hour
    assert run1.cost.total_usd == run2.cost.total_usd


def test_run_different_seeds_produce_different_results() -> None:
    tasks = [_make_task(f"t{i}") for i in range(10)]
    run_a = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=1)).run()
    run_b = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=2)).run()
    # Run IDs differ (different seeds)
    assert run_a.run_id != run_b.run_id


def test_run_with_task_id_filter() -> None:
    tasks = [_make_task(f"t{i}") for i in range(10)]
    bench = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=42, task_ids=["t0", "t3"]))
    run = bench.run()
    assert run.task_count == 2
    assert {r.task_id for r in run.records} == {"t0", "t3"}


def test_run_empty_tasks_produces_zero_metrics() -> None:
    bench = ReproducibleBenchmark(tasks=[], config=BenchmarkConfig(seed=42))
    run = bench.run()
    assert run.task_count == 0
    assert run.throughput.tasks_per_hour == pytest.approx(0.0)
    assert run.quality.pass_rate == pytest.approx(0.0)
    assert run.cost.total_usd == pytest.approx(0.0)


def test_run_has_timestamp() -> None:
    bench = ReproducibleBenchmark(tasks=[_make_task()])
    run = bench.run()
    assert run.timestamp != ""
    assert "T" in run.timestamp  # ISO format


def test_run_config_property() -> None:
    config = BenchmarkConfig(seed=7)
    bench = ReproducibleBenchmark(tasks=[], config=config)
    assert bench.config.seed == 7


# ---------------------------------------------------------------------------
# compare_to_baseline / RegressionReport
# ---------------------------------------------------------------------------


def test_compare_no_regression_when_identical() -> None:
    tasks = [_make_task(f"t{i}") for i in range(5)]
    bench = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=42))
    run = bench.run()
    report = bench.compare_to_baseline(current=run, baseline=run)
    assert not report.is_regression
    assert report.regressions == []


def test_compare_detects_throughput_regression() -> None:
    bench = ReproducibleBenchmark(tasks=[])
    baseline = _make_run(tasks_per_hour=100.0)
    # Drop throughput by 20% — exceeds 10% threshold
    current = _make_run(run_id="cur", tasks_per_hour=80.0)
    report = bench.compare_to_baseline(current=current, baseline=baseline)
    assert report.is_regression
    assert any("Throughput" in r for r in report.regressions)


def test_compare_does_not_flag_throughput_improvement() -> None:
    bench = ReproducibleBenchmark(tasks=[])
    baseline = _make_run(tasks_per_hour=100.0)
    current = _make_run(run_id="cur", tasks_per_hour=150.0)
    report = bench.compare_to_baseline(current=current, baseline=baseline)
    assert not report.is_regression


def test_compare_detects_cost_regression() -> None:
    bench = ReproducibleBenchmark(tasks=[])
    baseline = _make_run(per_task_usd=0.001)
    # 20% cost increase — exceeds 15% threshold
    current = _make_run(run_id="cur", per_task_usd=0.0012)
    report = bench.compare_to_baseline(current=current, baseline=baseline)
    assert report.is_regression
    assert any("Cost" in r for r in report.regressions)


def test_compare_does_not_flag_cost_reduction() -> None:
    bench = ReproducibleBenchmark(tasks=[])
    baseline = _make_run(per_task_usd=0.001)
    current = _make_run(run_id="cur", per_task_usd=0.0008)
    report = bench.compare_to_baseline(current=current, baseline=baseline)
    assert not report.is_regression


def test_compare_detects_quality_regression() -> None:
    bench = ReproducibleBenchmark(tasks=[])
    baseline = _make_run(pass_rate=0.90)
    # Drop by 6pp — exceeds 5pp threshold
    current = _make_run(run_id="cur", pass_rate=0.84)
    report = bench.compare_to_baseline(current=current, baseline=baseline)
    assert report.is_regression
    assert any("Quality" in r for r in report.regressions)


def test_compare_does_not_flag_quality_improvement() -> None:
    bench = ReproducibleBenchmark(tasks=[])
    baseline = _make_run(pass_rate=0.80)
    current = _make_run(run_id="cur", pass_rate=0.90)
    report = bench.compare_to_baseline(current=current, baseline=baseline)
    assert not report.is_regression


def test_compare_quality_at_threshold_boundary() -> None:
    bench = ReproducibleBenchmark(tasks=[])
    baseline = _make_run(pass_rate=0.90)
    # Exactly at threshold (5pp drop) — should NOT trigger (strictly greater)
    current = _make_run(run_id="cur", pass_rate=0.85)
    report = bench.compare_to_baseline(current=current, baseline=baseline)
    assert not report.is_regression


def test_compare_multiple_regressions_flagged() -> None:
    bench = ReproducibleBenchmark(tasks=[])
    baseline = _make_run(tasks_per_hour=100.0, per_task_usd=0.001, pass_rate=0.90)
    current = _make_run(run_id="cur", tasks_per_hour=70.0, per_task_usd=0.0015, pass_rate=0.80)
    report = bench.compare_to_baseline(current=current, baseline=baseline)
    assert report.is_regression
    assert len(report.regressions) >= 2


def test_compare_report_serializes() -> None:
    bench = ReproducibleBenchmark(tasks=[])
    baseline = _make_run()
    current = _make_run(run_id="cur")
    report = bench.compare_to_baseline(current=current, baseline=baseline)
    d = report.to_dict()
    assert "is_regression" in d
    assert "regressions" in d
    assert "throughput_delta_pct" in d


def test_compare_delta_signs_are_correct() -> None:
    bench = ReproducibleBenchmark(tasks=[])
    baseline = _make_run(tasks_per_hour=100.0, per_task_usd=0.001, pass_rate=0.80)
    # Faster, cheaper, better
    current = _make_run(run_id="cur", tasks_per_hour=120.0, per_task_usd=0.0008, pass_rate=0.90)
    report = bench.compare_to_baseline(current=current, baseline=baseline)
    assert report.throughput_delta_pct > 0  # faster is positive
    assert report.cost_delta_pct < 0  # cheaper is negative
    assert report.quality_delta_pp > 0  # better quality is positive


# ---------------------------------------------------------------------------
# Threshold constants
# ---------------------------------------------------------------------------


def test_threshold_constants_are_sane() -> None:
    assert 0 < THROUGHPUT_REGRESSION_THRESHOLD < 1
    assert 0 < COST_REGRESSION_THRESHOLD < 1
    assert QUALITY_REGRESSION_THRESHOLD_PP > 0


# ---------------------------------------------------------------------------
# save / load persistence
# ---------------------------------------------------------------------------


def test_save_creates_jsonl_file(tmp_path: Path) -> None:
    tasks = [_make_task(f"t{i}") for i in range(3)]
    bench = ReproducibleBenchmark(tasks=tasks)
    run = bench.run()
    path = bench.save(run, tmp_path)
    assert path.exists()
    assert path.suffix == ".jsonl"


def test_save_appends_multiple_runs(tmp_path: Path) -> None:
    tasks = [_make_task(f"t{i}") for i in range(3)]
    for seed in (1, 2, 3):
        bench2 = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=seed))
        bench2.save(bench2.run(), tmp_path)
    path = tmp_path / "benchmark_runs.jsonl"
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 3


def test_load_returns_empty_for_missing_file(tmp_path: Path) -> None:
    runs = ReproducibleBenchmark.load(tmp_path / "does_not_exist.jsonl")
    assert runs == []


def test_save_load_roundtrip(tmp_path: Path) -> None:
    tasks = [_make_task(f"t{i}") for i in range(5)]
    bench = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=42))
    run = bench.run()
    path = bench.save(run, tmp_path)
    loaded = ReproducibleBenchmark.load(path)
    assert len(loaded) == 1
    assert loaded[0].run_id == run.run_id
    assert loaded[0].seed == 42
    assert abs(loaded[0].quality.pass_rate - run.quality.pass_rate) < 1e-6
    assert abs(loaded[0].throughput.tasks_per_hour - run.throughput.tasks_per_hour) < 0.01


def test_load_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "benchmark_runs.jsonl"
    path.write_text('{"bad": "data"}\nnot json at all\n')
    runs = ReproducibleBenchmark.load(path)
    # Both lines malformed (missing required keys) — should return empty or skip
    assert isinstance(runs, list)


# ---------------------------------------------------------------------------
# run_and_compare
# ---------------------------------------------------------------------------


def test_run_and_compare_without_baseline(tmp_path: Path) -> None:
    tasks = [_make_task(f"t{i}") for i in range(4)]
    bench = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=1))
    run, report = bench.run_and_compare()
    assert isinstance(run, BenchmarkRun)
    assert report is None  # No baseline configured


def test_run_and_compare_saves_to_output_dir(tmp_path: Path) -> None:
    tasks = [_make_task(f"t{i}") for i in range(3)]
    bench = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=42, output_dir=tmp_path))
    bench.run_and_compare()
    assert (tmp_path / "benchmark_runs.jsonl").exists()


def test_run_and_compare_with_baseline(tmp_path: Path) -> None:
    tasks = [_make_task(f"t{i}") for i in range(5)]

    # Save a baseline run
    bench1 = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=42, output_dir=tmp_path))
    bench1.run_and_compare()

    baseline_path = tmp_path / "benchmark_runs.jsonl"
    assert baseline_path.exists()

    # Run with baseline comparison
    bench2 = ReproducibleBenchmark(
        tasks=tasks,
        config=BenchmarkConfig(seed=42, baseline_path=baseline_path, output_dir=tmp_path),
    )
    run, report = bench2.run_and_compare()
    assert isinstance(run, BenchmarkRun)
    assert isinstance(report, RegressionReport)
    # Same seed → no regression
    assert not report.is_regression


# ---------------------------------------------------------------------------
# BenchmarkRun serialization
# ---------------------------------------------------------------------------


def test_benchmark_run_to_dict_has_required_keys() -> None:
    tasks = [_make_task()]
    bench = ReproducibleBenchmark(tasks=tasks)
    run = bench.run()
    d = run.to_dict()
    for key in ("run_id", "timestamp", "seed", "task_count", "throughput", "cost", "quality", "records"):
        assert key in d


def test_benchmark_run_from_dict_roundtrip() -> None:
    tasks = [_make_task(f"t{i}") for i in range(4)]
    bench = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=99))
    run = bench.run()
    d = run.to_dict()
    restored = BenchmarkRun.from_dict(d)
    assert restored.run_id == run.run_id
    assert restored.seed == run.seed
    assert restored.task_count == run.task_count


def test_task_run_record_to_dict() -> None:
    record = _make_record()
    d = record.to_dict()
    assert set(d.keys()) == {"task_id", "elapsed_s", "cost_usd", "tokens", "passed", "verified"}
    assert d["passed"] is True
    assert d["verified"] is True
