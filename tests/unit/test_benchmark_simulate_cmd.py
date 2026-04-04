"""Unit tests for the bernstein benchmark simulate CLI command.

Covers:
- Loading standard benchmark task YAML files from templates/benchmarks/
- The benchmark simulate command producing metrics
- Regression detection path
- Save/no-save flag behaviour
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from bernstein.benchmark.comparative import BenchmarkTask, load_benchmark_tasks
from bernstein.benchmark.reproducible import (
    BenchmarkConfig,
    BenchmarkRun,
    ReproducibleBenchmark,
    ThroughputMetrics,
    CostMetrics,
    QualityMetrics,
)
from bernstein.cli.eval_benchmark_cmd import benchmark_simulate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_yaml(directory: Path, task_id: str, task_type: str = "bugfix") -> Path:
    """Write a minimal benchmark task YAML into *directory*."""
    path = directory / f"{task_id}.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "task_id": task_id,
                "description": f"Description for {task_id}",
                "task_type": task_type,
                "files": ["src/foo.py"],
                "expected_outcome": "Tests pass",
            }
        ),
        encoding="utf-8",
    )
    return path


def _make_run(
    run_id: str = "abc123",
    tasks_per_hour: float = 100.0,
    per_task_usd: float = 0.001,
    pass_rate: float = 0.85,
    task_count: int = 5,
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
# load_benchmark_tasks: standard task YAMLs
# ---------------------------------------------------------------------------


def test_load_benchmark_tasks_from_templates(tmp_path: Path) -> None:
    """load_benchmark_tasks returns tasks for all valid YAML files."""
    for tid in ("t01", "t02", "t03"):
        _make_task_yaml(tmp_path, tid)
    tasks = load_benchmark_tasks(tmp_path)
    assert len(tasks) == 3
    assert all(isinstance(t, BenchmarkTask) for t in tasks)


def test_load_benchmark_tasks_sorted_by_id(tmp_path: Path) -> None:
    """Tasks are returned in sorted task_id order."""
    for tid in ("z-task", "a-task", "m-task"):
        _make_task_yaml(tmp_path, tid)
    tasks = load_benchmark_tasks(tmp_path)
    ids = [t.task_id for t in tasks]
    assert ids == sorted(ids)


def test_load_benchmark_tasks_skips_invalid_yaml(tmp_path: Path) -> None:
    """Malformed YAML files are skipped silently."""
    _make_task_yaml(tmp_path, "valid")
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: valid: yaml: [unclosed", encoding="utf-8")
    tasks = load_benchmark_tasks(tmp_path)
    # Only the valid task should be loaded
    assert len(tasks) == 1
    assert tasks[0].task_id == "valid"


def test_load_benchmark_tasks_missing_dir() -> None:
    """Missing directory returns empty list without raising."""
    tasks = load_benchmark_tasks(Path("/does/not/exist"))
    assert tasks == []


def test_standard_templates_dir_has_tasks() -> None:
    """The templates/benchmarks/ directory ships task YAML files."""
    templates_dir = Path("templates/benchmarks")
    if not templates_dir.is_dir():
        pytest.skip("templates/benchmarks not present in this worktree")
    tasks = load_benchmark_tasks(templates_dir)
    assert len(tasks) >= 5, f"Expected ≥5 standard tasks, got {len(tasks)}"


# ---------------------------------------------------------------------------
# simulate CLI command: basic smoke
# ---------------------------------------------------------------------------


def test_benchmark_simulate_exits_zero(tmp_path: Path) -> None:
    """simulate command exits 0 when tasks are found and run."""
    for tid in ("t1", "t2", "t3"):
        _make_task_yaml(tmp_path, tid)

    runner = CliRunner()
    result = runner.invoke(
        benchmark_simulate,
        ["--tasks-dir", str(tmp_path), "--seed", "42", "--no-save"],
    )
    assert result.exit_code == 0, result.output


def test_benchmark_simulate_shows_metrics(tmp_path: Path) -> None:
    """simulate command output contains key metric labels."""
    for tid in ("t1", "t2"):
        _make_task_yaml(tmp_path, tid)

    runner = CliRunner()
    result = runner.invoke(
        benchmark_simulate,
        ["--tasks-dir", str(tmp_path), "--seed", "7", "--no-save"],
    )
    assert result.exit_code == 0, result.output
    assert "Tasks/hour" in result.output
    assert "Pass rate" in result.output
    assert "Cost/task" in result.output


def test_benchmark_simulate_missing_dir_exits_nonzero() -> None:
    """simulate command exits 1 when tasks-dir does not exist."""
    runner = CliRunner()
    result = runner.invoke(
        benchmark_simulate,
        ["--tasks-dir", "/no/such/dir", "--no-save"],
    )
    assert result.exit_code != 0


def test_benchmark_simulate_empty_dir_exits_nonzero(tmp_path: Path) -> None:
    """simulate command exits 1 when tasks-dir is empty."""
    runner = CliRunner()
    result = runner.invoke(
        benchmark_simulate,
        ["--tasks-dir", str(tmp_path), "--no-save"],
    )
    assert result.exit_code != 0


def test_benchmark_simulate_task_id_filter(tmp_path: Path) -> None:
    """--task-id filters down to the requested subset."""
    for tid in ("alpha", "beta", "gamma"):
        _make_task_yaml(tmp_path, tid)

    runner = CliRunner()
    result = runner.invoke(
        benchmark_simulate,
        ["--tasks-dir", str(tmp_path), "--seed", "1", "--task-id", "alpha", "--no-save"],
    )
    assert result.exit_code == 0, result.output
    assert "Tasks run" in result.output


def test_benchmark_simulate_saves_jsonl(tmp_path: Path) -> None:
    """--save writes benchmark_runs.jsonl to .sdd/benchmarks/."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for tid in ("t1", "t2"):
        _make_task_yaml(tasks_dir, tid)

    # Point .sdd into tmp_path so we don't pollute the real .sdd/
    import os

    orig_dir = Path.cwd()
    os.chdir(tmp_path)
    try:
        runner = CliRunner()
        result = runner.invoke(
            benchmark_simulate,
            ["--tasks-dir", str(tasks_dir), "--seed", "42", "--save"],
        )
        assert result.exit_code == 0, result.output
        out_path = tmp_path / ".sdd" / "benchmarks" / "benchmark_runs.jsonl"
        assert out_path.exists(), f"Expected {out_path} to exist"
    finally:
        os.chdir(orig_dir)


# ---------------------------------------------------------------------------
# simulate CLI command: regression detection
# ---------------------------------------------------------------------------


def test_benchmark_simulate_no_regression_when_identical_baseline(tmp_path: Path) -> None:
    """Identical seed produces no regression vs saved baseline."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for tid in ("t1", "t2", "t3"):
        _make_task_yaml(tasks_dir, tid)

    tasks = load_benchmark_tasks(tasks_dir)
    bench = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=42))
    baseline_run = bench.run()
    baseline_path = tmp_path / "baseline.jsonl"
    bench.save(baseline_run, tmp_path)
    baseline_path = tmp_path / "benchmark_runs.jsonl"

    runner = CliRunner()
    result = runner.invoke(
        benchmark_simulate,
        [
            "--tasks-dir",
            str(tasks_dir),
            "--seed",
            "42",
            "--baseline",
            str(baseline_path),
            "--no-save",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No regression" in result.output


def test_benchmark_simulate_detects_regression(tmp_path: Path) -> None:
    """A significantly worse baseline triggers regression exit code 1."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for tid in ("t1", "t2", "t3", "t4", "t5"):
        _make_task_yaml(tasks_dir, tid)

    # Manually write a baseline with much higher throughput so current run looks worse
    tasks = load_benchmark_tasks(tasks_dir)
    bench = ReproducibleBenchmark(tasks=tasks, config=BenchmarkConfig(seed=42))
    # Build a "great" baseline run
    great_run = _make_run(run_id="base123", tasks_per_hour=9999.0, per_task_usd=0.00001, pass_rate=0.99)
    baseline_path = tmp_path / "baseline.jsonl"
    bench.save(great_run, tmp_path)
    baseline_path = tmp_path / "benchmark_runs.jsonl"

    runner = CliRunner()
    result = runner.invoke(
        benchmark_simulate,
        [
            "--tasks-dir",
            str(tasks_dir),
            "--seed",
            "42",
            "--baseline",
            str(baseline_path),
            "--no-save",
        ],
    )
    assert result.exit_code == 1
    assert "Regression detected" in result.output


def test_benchmark_simulate_run_id_in_output(tmp_path: Path) -> None:
    """Output contains the run ID for traceability."""
    for tid in ("t1",):
        _make_task_yaml(tmp_path, tid)

    runner = CliRunner()
    result = runner.invoke(
        benchmark_simulate,
        ["--tasks-dir", str(tmp_path), "--seed", "42", "--no-save"],
    )
    assert result.exit_code == 0, result.output
    assert "Run ID" in result.output


def test_benchmark_simulate_deterministic_across_invocations(tmp_path: Path) -> None:
    """Same seed and tasks produce identical output twice."""
    for tid in ("t1", "t2"):
        _make_task_yaml(tmp_path, tid)

    runner = CliRunner()
    args = ["--tasks-dir", str(tmp_path), "--seed", "99", "--no-save"]
    r1 = runner.invoke(benchmark_simulate, args)
    r2 = runner.invoke(benchmark_simulate, args)
    assert r1.exit_code == 0
    assert r2.exit_code == 0
    # Key metric lines should be identical
    assert r1.output == r2.output
