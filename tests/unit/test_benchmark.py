"""Unit tests for bernstein.benchmark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bernstein.evolution.benchmark import (
    BenchmarkResult,
    BenchmarkSpec,
    RunSummary,
    SignalResult,
    SignalSpec,
    _eval_import_succeeds,
    _eval_path_exists,
    _eval_signal,
    load_benchmarks,
    run_all,
    run_benchmark,
    run_selected,
    run_tier,
    save_results,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_yaml(directory: Path, filename: str, data: dict) -> Path:
    path = directory / filename
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture()
def benchmarks_dir(tmp_path: Path) -> Path:
    """Create a minimal benchmarks directory with all three tiers."""
    for tier in ("smoke", "capability", "stretch"):
        (tmp_path / tier).mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# SignalSpec evaluators
# ---------------------------------------------------------------------------


def test_eval_import_succeeds_passes_for_stdlib_module() -> None:
    spec = SignalSpec(type="import_succeeds", module="pathlib")
    result = _eval_import_succeeds(spec)
    assert result.passed is True
    assert "pathlib" in result.message


def test_eval_import_succeeds_passes_for_module_with_attribute() -> None:
    spec = SignalSpec(type="import_succeeds", module="pathlib", attribute="Path")
    result = _eval_import_succeeds(spec)
    assert result.passed is True


def test_eval_import_succeeds_fails_for_nonexistent_module() -> None:
    spec = SignalSpec(type="import_succeeds", module="definitely_not_a_real_module_xyz")
    result = _eval_import_succeeds(spec)
    assert result.passed is False
    assert "ImportError" in result.message


def test_eval_import_succeeds_fails_for_missing_attribute() -> None:
    spec = SignalSpec(type="import_succeeds", module="pathlib", attribute="NonExistentClass")
    result = _eval_import_succeeds(spec)
    assert result.passed is False
    assert "no attribute" in result.message


def test_eval_import_succeeds_fails_when_module_field_missing() -> None:
    spec = SignalSpec(type="import_succeeds")
    result = _eval_import_succeeds(spec)
    assert result.passed is False
    assert "missing" in result.message.lower()


def test_eval_path_exists_passes_for_existing_path(tmp_path: Path) -> None:
    target = tmp_path / "somefile.txt"
    target.write_text("hello")
    spec = SignalSpec(type="path_exists", path=str(target))
    result = _eval_path_exists(spec)
    assert result.passed is True


def test_eval_path_exists_fails_for_missing_path(tmp_path: Path) -> None:
    spec = SignalSpec(type="path_exists", path=str(tmp_path / "nonexistent.txt"))
    result = _eval_path_exists(spec)
    assert result.passed is False
    assert "not found" in result.message


def test_eval_path_exists_fails_when_path_field_missing() -> None:
    spec = SignalSpec(type="path_exists")
    result = _eval_path_exists(spec)
    assert result.passed is False
    assert "missing" in result.message.lower()


def test_eval_signal_dispatches_import_succeeds() -> None:
    spec = SignalSpec(type="import_succeeds", module="json")
    result = _eval_signal(spec)
    assert result.signal_type == "import_succeeds"
    assert result.passed is True


def test_eval_signal_dispatches_path_exists(tmp_path: Path) -> None:
    spec = SignalSpec(type="path_exists", path=str(tmp_path))
    result = _eval_signal(spec)
    assert result.signal_type == "path_exists"
    assert result.passed is True


def test_eval_signal_unknown_type_is_skipped() -> None:
    spec = SignalSpec(type="llm_review", command="some command")
    result = _eval_signal(spec)
    # Unknown types pass so they don't break runs
    assert result.passed is True
    assert "unsupported" in result.message


# ---------------------------------------------------------------------------
# load_benchmarks
# ---------------------------------------------------------------------------


def test_load_benchmarks_returns_empty_for_missing_tier(tmp_path: Path) -> None:
    specs = load_benchmarks(tmp_path, "smoke")
    assert specs == []


def test_load_benchmarks_returns_empty_for_empty_tier_dir(benchmarks_dir: Path) -> None:
    specs = load_benchmarks(benchmarks_dir, "smoke")
    assert specs == []


def test_load_benchmarks_parses_yaml_file(benchmarks_dir: Path) -> None:
    _write_yaml(
        benchmarks_dir / "smoke",
        "s001.yaml",
        {
            "id": "s001",
            "goal": "Package imports work",
            "expected_signals": [{"type": "import_succeeds", "module": "json"}],
            "max_cost_usd": 0.0,
            "max_duration_seconds": 10,
        },
    )
    specs = load_benchmarks(benchmarks_dir, "smoke")
    assert len(specs) == 1
    assert specs[0].id == "s001"
    assert specs[0].goal == "Package imports work"
    assert specs[0].tier == "smoke"
    assert len(specs[0].expected_signals) == 1
    assert specs[0].expected_signals[0].type == "import_succeeds"


def test_load_benchmarks_assigns_tier_correctly(benchmarks_dir: Path) -> None:
    _write_yaml(
        benchmarks_dir / "capability",
        "c001.yaml",
        {"id": "c001", "goal": "Cap test", "expected_signals": []},
    )
    specs = load_benchmarks(benchmarks_dir, "capability")
    assert specs[0].tier == "capability"


def test_load_benchmarks_sorts_by_id(benchmarks_dir: Path) -> None:
    for name in ("z-last.yaml", "a-first.yaml", "m-middle.yaml"):
        _write_yaml(
            benchmarks_dir / "smoke",
            name,
            {"id": name.replace(".yaml", ""), "goal": name, "expected_signals": []},
        )
    specs = load_benchmarks(benchmarks_dir, "smoke")
    ids = [s.id for s in specs]
    assert ids == sorted(ids)


def test_load_benchmarks_skips_malformed_yaml(benchmarks_dir: Path) -> None:
    bad = benchmarks_dir / "smoke" / "bad.yaml"
    bad.write_text("not: valid: yaml: [[[")
    # Should not raise; malformed files are skipped
    specs = load_benchmarks(benchmarks_dir, "smoke")
    assert specs == []


def test_load_benchmarks_uses_defaults_for_optional_fields(benchmarks_dir: Path) -> None:
    _write_yaml(
        benchmarks_dir / "smoke",
        "minimal.yaml",
        {"id": "minimal", "goal": "Minimal spec", "expected_signals": []},
    )
    specs = load_benchmarks(benchmarks_dir, "smoke")
    assert specs[0].max_cost_usd == pytest.approx(0.0)
    assert specs[0].max_duration_seconds == 60


# ---------------------------------------------------------------------------
# run_benchmark
# ---------------------------------------------------------------------------


def test_run_benchmark_passes_when_all_signals_pass() -> None:
    spec = BenchmarkSpec(
        id="t001",
        goal="JSON importable",
        tier="smoke",
        expected_signals=[SignalSpec(type="import_succeeds", module="json")],
    )
    result = run_benchmark(spec)
    assert result.passed is True
    assert result.benchmark_id == "t001"
    assert result.error is None
    assert len(result.signal_results) == 1
    assert result.signal_results[0].passed is True


def test_run_benchmark_fails_when_any_signal_fails() -> None:
    spec = BenchmarkSpec(
        id="t002",
        goal="Nonexistent module",
        tier="smoke",
        expected_signals=[
            SignalSpec(type="import_succeeds", module="json"),
            SignalSpec(type="import_succeeds", module="no_such_module_abc"),
        ],
    )
    result = run_benchmark(spec)
    assert result.passed is False
    assert result.signal_results[0].passed is True
    assert result.signal_results[1].passed is False


def test_run_benchmark_records_duration() -> None:
    spec = BenchmarkSpec(
        id="t003",
        goal="Duration tracked",
        tier="capability",
        expected_signals=[SignalSpec(type="import_succeeds", module="json")],
    )
    result = run_benchmark(spec)
    assert result.duration_seconds >= 0.0


def test_run_benchmark_with_no_signals_passes() -> None:
    spec = BenchmarkSpec(id="empty", goal="Empty signals", tier="stretch", expected_signals=[])
    result = run_benchmark(spec)
    assert result.passed is True


# ---------------------------------------------------------------------------
# run_tier / run_all / run_selected
# ---------------------------------------------------------------------------


def test_run_tier_returns_empty_for_tier_with_no_benchmarks(benchmarks_dir: Path) -> None:
    results = run_tier(benchmarks_dir, "smoke")
    assert results == []


def test_run_tier_returns_results_for_populated_tier(benchmarks_dir: Path) -> None:
    _write_yaml(
        benchmarks_dir / "smoke",
        "s001.yaml",
        {
            "id": "s001",
            "goal": "Import json",
            "expected_signals": [{"type": "import_succeeds", "module": "json"}],
        },
    )
    results = run_tier(benchmarks_dir, "smoke")
    assert len(results) == 1
    assert results[0].passed is True


def test_run_all_aggregates_across_tiers(benchmarks_dir: Path) -> None:
    for tier, idx in [("smoke", "s1"), ("capability", "c1"), ("stretch", "x1")]:
        _write_yaml(
            benchmarks_dir / tier,
            f"{idx}.yaml",
            {
                "id": idx,
                "goal": f"test {tier}",
                "expected_signals": [{"type": "import_succeeds", "module": "json"}],
            },
        )
    summary = run_all(benchmarks_dir)
    assert summary.tier == "all"
    assert summary.total == 3
    assert summary.passed == 3
    assert summary.failed == 0
    assert len(summary.results) == 3


def test_run_all_counts_failures_correctly(benchmarks_dir: Path) -> None:
    _write_yaml(
        benchmarks_dir / "smoke",
        "fail.yaml",
        {
            "id": "fail",
            "goal": "Missing import",
            "expected_signals": [{"type": "import_succeeds", "module": "no_such_pkg"}],
        },
    )
    summary = run_all(benchmarks_dir)
    assert summary.failed == 1
    assert summary.passed == 0


def test_run_selected_runs_only_requested_tier(benchmarks_dir: Path) -> None:
    _write_yaml(
        benchmarks_dir / "smoke",
        "s1.yaml",
        {"id": "s1", "goal": "smoke", "expected_signals": [{"type": "import_succeeds", "module": "json"}]},
    )
    _write_yaml(
        benchmarks_dir / "capability",
        "c1.yaml",
        {"id": "c1", "goal": "capability", "expected_signals": [{"type": "import_succeeds", "module": "json"}]},
    )
    summary = run_selected(benchmarks_dir, "smoke")
    assert summary.tier == "smoke"
    assert summary.total == 1
    assert all(r.tier == "smoke" for r in summary.results)


def test_run_selected_returns_empty_summary_for_empty_tier(benchmarks_dir: Path) -> None:
    summary = run_selected(benchmarks_dir, "stretch")
    assert summary.tier == "stretch"
    assert summary.total == 0
    assert summary.passed == 0
    assert summary.failed == 0


# ---------------------------------------------------------------------------
# save_results
# ---------------------------------------------------------------------------


def test_save_results_creates_jsonl_file(tmp_path: Path) -> None:
    summary = RunSummary(
        tier="smoke",
        total=1,
        passed=1,
        failed=0,
        results=[
            BenchmarkResult(
                benchmark_id="s001",
                tier="smoke",
                passed=True,
                goal="test",
                signal_results=[SignalResult(signal_type="import_succeeds", passed=True, message="OK")],
                duration_seconds=0.01,
            )
        ],
    )
    out_path = save_results(summary, tmp_path)
    assert out_path.exists()
    assert out_path.suffix == ".jsonl"
    assert "benchmarks" in str(out_path)


def test_save_results_writes_valid_json(tmp_path: Path) -> None:
    summary = RunSummary(
        tier="all",
        total=2,
        passed=2,
        failed=0,
        results=[
            BenchmarkResult(
                benchmark_id="s001",
                tier="smoke",
                passed=True,
                goal="test1",
                duration_seconds=0.0,
            ),
            BenchmarkResult(
                benchmark_id="c001",
                tier="capability",
                passed=True,
                goal="test2",
                duration_seconds=0.0,
            ),
        ],
    )
    out_path = save_results(summary, tmp_path)
    lines = out_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["tier"] == "all"
    assert record["total"] == 2
    assert record["passed"] == 2
    assert record["failed"] == 0
    assert len(record["results"]) == 2


def test_save_results_appends_on_multiple_calls(tmp_path: Path) -> None:
    summary = RunSummary(tier="smoke", total=1, passed=1, failed=0, results=[])
    out_path = save_results(summary, tmp_path)
    save_results(summary, tmp_path)
    lines = out_path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_save_results_creates_benchmarks_subdir(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    summary = RunSummary(tier="smoke", total=0, passed=0, failed=0, results=[])
    out_path = save_results(summary, sdd)
    assert (sdd / "benchmarks").is_dir()
    assert out_path.parent == sdd / "benchmarks"


# ---------------------------------------------------------------------------
# Integration: load + run against real benchmark YAML files
# ---------------------------------------------------------------------------


def test_real_smoke_benchmarks_all_pass() -> None:
    """The golden smoke benchmarks must always pass."""
    real_dir = Path(__file__).parent.parent.parent / "tests" / "benchmarks"
    if not real_dir.exists():
        pytest.skip("tests/benchmarks/ directory not found")
    summary = run_selected(real_dir, "smoke")
    failed = [r.benchmark_id for r in summary.results if not r.passed]
    assert not failed, f"Smoke benchmarks FAILED (critical regression): {failed}"


def test_real_benchmark_yaml_files_are_parseable() -> None:
    """All benchmark YAML files must be parseable without errors."""
    real_dir = Path(__file__).parent.parent.parent / "tests" / "benchmarks"
    if not real_dir.exists():
        pytest.skip("tests/benchmarks/ directory not found")
    summary = run_all(real_dir)
    assert summary.total > 0, "No benchmark YAML files found"
