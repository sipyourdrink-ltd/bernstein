"""Tests for comparative benchmark — YAML loading, report aggregation, markdown."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bernstein.benchmark.comparative import (
    BenchmarkResult,
    BenchmarkTask,
    ComparativeBenchmark,
    compute_report,
    load_benchmark_tasks,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_tasks() -> list[BenchmarkTask]:
    """Three tasks covering different task types."""
    return [
        BenchmarkTask(
            task_id="task-bugfix",
            description="Fix off-by-one",
            task_type="bugfix",
            files=["src/utils.py"],
            expected_outcome="All tests pass.",
        ),
        BenchmarkTask(
            task_id="task-refactor",
            description="Extract function",
            task_type="refactor",
            files=["src/handler.py"],
            expected_outcome="Handler refactored.",
        ),
        BenchmarkTask(
            task_id="task-test",
            description="Add unit tests",
            task_type="test",
            files=["tests/test_foo.py"],
            expected_outcome="8 new tests.",
        ),
    ]


@pytest.fixture()
def sample_results() -> list[BenchmarkResult]:
    """Pre-built results for both modes."""
    return [
        BenchmarkResult(
            task_id="task-1",
            mode="single",
            wall_time_seconds=10.5,
            cost_usd=0.05,
            tokens_used=1200,
            success=True,
            verification_passed=True,
        ),
        BenchmarkResult(
            task_id="task-1",
            mode="orchestrated",
            wall_time_seconds=8.0,
            cost_usd=0.12,
            tokens_used=3200,
            success=True,
            verification_passed=True,
        ),
        BenchmarkResult(
            task_id="task-2",
            mode="single",
            wall_time_seconds=15.0,
            cost_usd=0.08,
            tokens_used=2000,
            success=False,
            verification_passed=False,
        ),
        BenchmarkResult(
            task_id="task-2",
            mode="orchestrated",
            wall_time_seconds=12.0,
            cost_usd=0.20,
            tokens_used=5000,
            success=True,
            verification_passed=True,
        ),
    ]


@pytest.fixture()
def yaml_tasks_dir(tmp_path: Path) -> Path:
    """Directory with two YAML benchmark task files."""
    bugfix = {
        "task_id": "fix-bug",
        "description": "Fix the bug",
        "task_type": "bugfix",
        "files": ["src/main.py"],
        "expected_outcome": "Bug is fixed.",
    }
    docs = {
        "task_id": "add-docs",
        "description": "Add docstrings",
        "task_type": "docs",
        "files": ["src/api.py", "src/models.py"],
        "expected_outcome": "All public functions have docstrings.",
    }
    (tmp_path / "fix-bug.yaml").write_text(yaml.dump(bugfix), encoding="utf-8")
    (tmp_path / "add-docs.yaml").write_text(yaml.dump(docs), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# TestBenchmarkTask
# ---------------------------------------------------------------------------


class TestBenchmarkTask:
    """Tests for BenchmarkTask construction and parsing."""

    def test_from_dict_valid(self) -> None:
        raw = {
            "task_id": "t1",
            "description": "Do something",
            "task_type": "bugfix",
            "files": ["a.py"],
            "expected_outcome": "It works.",
        }
        task = BenchmarkTask.from_dict(raw)
        assert task.task_id == "t1"
        assert task.task_type == "bugfix"
        assert task.files == ["a.py"]

    def test_from_dict_missing_field(self) -> None:
        with pytest.raises(KeyError):
            BenchmarkTask.from_dict({"task_id": "t1"})

    def test_from_dict_empty_files(self) -> None:
        raw = {
            "task_id": "t2",
            "description": "No files",
            "task_type": "docs",
            "expected_outcome": "Done.",
        }
        task = BenchmarkTask.from_dict(raw)
        assert task.files == []

    def test_frozen(self) -> None:
        task = BenchmarkTask(
            task_id="t3",
            description="Frozen",
            task_type="test",
            files=[],
            expected_outcome="Immutable.",
        )
        with pytest.raises(AttributeError):
            task.task_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestLoadBenchmarkTasks
# ---------------------------------------------------------------------------


class TestLoadBenchmarkTasks:
    """Tests for YAML-based task loading."""

    def test_loads_from_directory(self, yaml_tasks_dir: Path) -> None:
        tasks = load_benchmark_tasks(yaml_tasks_dir)
        assert len(tasks) == 2
        ids = [t.task_id for t in tasks]
        assert "add-docs" in ids
        assert "fix-bug" in ids

    def test_sorted_by_task_id(self, yaml_tasks_dir: Path) -> None:
        tasks = load_benchmark_tasks(yaml_tasks_dir)
        assert tasks[0].task_id == "add-docs"
        assert tasks[1].task_id == "fix-bug"

    def test_empty_directory(self, tmp_path: Path) -> None:
        tasks = load_benchmark_tasks(tmp_path)
        assert tasks == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        tasks = load_benchmark_tasks(tmp_path / "nope")
        assert tasks == []

    def test_skips_non_yaml(self, yaml_tasks_dir: Path) -> None:
        (yaml_tasks_dir / "readme.md").write_text("# Not a task")
        tasks = load_benchmark_tasks(yaml_tasks_dir)
        assert len(tasks) == 2

    def test_skips_invalid_yaml(self, yaml_tasks_dir: Path) -> None:
        (yaml_tasks_dir / "bad.yaml").write_text("task_id: only-one-field")
        tasks = load_benchmark_tasks(yaml_tasks_dir)
        assert len(tasks) == 2

    def test_skips_empty_yaml(self, yaml_tasks_dir: Path) -> None:
        (yaml_tasks_dir / "empty.yaml").write_text("")
        tasks = load_benchmark_tasks(yaml_tasks_dir)
        assert len(tasks) == 2


# ---------------------------------------------------------------------------
# TestComputeReport
# ---------------------------------------------------------------------------


class TestComputeReport:
    """Tests for report summary aggregation."""

    def test_aggregates_by_mode(self, sample_results: list[BenchmarkResult]) -> None:
        report = compute_report(sample_results)
        assert "single" in report.summary
        assert "orchestrated" in report.summary

    def test_success_rate(self, sample_results: list[BenchmarkResult]) -> None:
        report = compute_report(sample_results)
        assert report.summary["single"].success_rate == 0.5
        assert report.summary["orchestrated"].success_rate == 1.0

    def test_total_tasks(self, sample_results: list[BenchmarkResult]) -> None:
        report = compute_report(sample_results)
        assert report.summary["single"].total_tasks == 2
        assert report.summary["orchestrated"].total_tasks == 2

    def test_cost_aggregation(self, sample_results: list[BenchmarkResult]) -> None:
        report = compute_report(sample_results)
        single = report.summary["single"]
        assert single.total_cost_usd == pytest.approx(0.13, abs=1e-4)
        assert single.avg_cost_usd == pytest.approx(0.065, abs=1e-4)

    def test_token_aggregation(self, sample_results: list[BenchmarkResult]) -> None:
        report = compute_report(sample_results)
        assert report.summary["single"].total_tokens == 3200
        assert report.summary["orchestrated"].total_tokens == 8200

    def test_wall_time_stats(self, sample_results: list[BenchmarkResult]) -> None:
        report = compute_report(sample_results)
        single = report.summary["single"]
        assert single.avg_wall_time == pytest.approx(12.75, abs=1e-2)
        assert single.median_wall_time == pytest.approx(12.75, abs=1e-2)

    def test_verification_rate(self, sample_results: list[BenchmarkResult]) -> None:
        report = compute_report(sample_results)
        assert report.summary["single"].verification_rate == 0.5
        assert report.summary["orchestrated"].verification_rate == 1.0

    def test_empty_results(self) -> None:
        report = compute_report([])
        assert report.results == []
        assert report.summary == {}

    def test_single_mode_only(self) -> None:
        results = [
            BenchmarkResult(
                task_id="t",
                mode="single",
                wall_time_seconds=5.0,
                cost_usd=0.01,
                tokens_used=100,
                success=True,
                verification_passed=True,
            )
        ]
        report = compute_report(results)
        assert "single" in report.summary
        assert "orchestrated" not in report.summary


# ---------------------------------------------------------------------------
# TestBenchmarkResult
# ---------------------------------------------------------------------------


class TestBenchmarkResult:
    """Tests for BenchmarkResult serialization."""

    def test_to_dict(self) -> None:
        r = BenchmarkResult(
            task_id="t1",
            mode="single",
            wall_time_seconds=10.123,
            cost_usd=0.04567,
            tokens_used=500,
            success=True,
            verification_passed=False,
        )
        d = r.to_dict()
        assert d["task_id"] == "t1"
        assert d["mode"] == "single"
        assert d["wall_time_seconds"] == 10.12
        assert d["cost_usd"] == 0.0457
        assert d["tokens_used"] == 500
        assert d["success"] is True
        assert d["verification_passed"] is False


# ---------------------------------------------------------------------------
# TestMarkdownReport
# ---------------------------------------------------------------------------


class TestMarkdownReport:
    """Tests for markdown report generation."""

    def test_contains_header(self, sample_tasks: list[BenchmarkTask], tmp_path: Path) -> None:
        suite = ComparativeBenchmark(tasks=sample_tasks, workdir=tmp_path)
        report = compute_report([])
        md = suite.generate_markdown_report(report)
        assert "# Comparative Benchmark Report" in md

    def test_contains_per_task_table(
        self,
        sample_tasks: list[BenchmarkTask],
        sample_results: list[BenchmarkResult],
        tmp_path: Path,
    ) -> None:
        suite = ComparativeBenchmark(tasks=sample_tasks, workdir=tmp_path)
        report = compute_report(sample_results)
        md = suite.generate_markdown_report(report)
        assert "## Per-Task Results" in md
        assert "| Task ID |" in md
        assert "task-1" in md
        assert "task-2" in md

    def test_contains_summary_table(
        self,
        sample_tasks: list[BenchmarkTask],
        sample_results: list[BenchmarkResult],
        tmp_path: Path,
    ) -> None:
        suite = ComparativeBenchmark(tasks=sample_tasks, workdir=tmp_path)
        report = compute_report(sample_results)
        md = suite.generate_markdown_report(report)
        assert "## Mode Comparison Summary" in md
        assert "Success Rate" in md
        assert "Total Cost" in md

    def test_success_marked_yes_no(
        self,
        sample_tasks: list[BenchmarkTask],
        sample_results: list[BenchmarkResult],
        tmp_path: Path,
    ) -> None:
        suite = ComparativeBenchmark(tasks=sample_tasks, workdir=tmp_path)
        report = compute_report(sample_results)
        md = suite.generate_markdown_report(report)
        assert "Yes" in md
        assert "No" in md

    def test_empty_report_no_summary(self, sample_tasks: list[BenchmarkTask], tmp_path: Path) -> None:
        suite = ComparativeBenchmark(tasks=sample_tasks, workdir=tmp_path)
        report = compute_report([])
        md = suite.generate_markdown_report(report)
        assert "## Mode Comparison Summary" not in md


# ---------------------------------------------------------------------------
# TestComparativeBenchmark
# ---------------------------------------------------------------------------


class TestComparativeBenchmark:
    """Tests for the ComparativeBenchmark suite runner."""

    def test_tasks_property(self, sample_tasks: list[BenchmarkTask], tmp_path: Path) -> None:
        suite = ComparativeBenchmark(tasks=sample_tasks, workdir=tmp_path)
        assert len(suite.tasks) == 3
        assert suite.tasks is not sample_tasks

    def test_run_suite_default_modes(self, sample_tasks: list[BenchmarkTask], tmp_path: Path) -> None:
        suite = ComparativeBenchmark(tasks=sample_tasks, workdir=tmp_path)
        report = suite.run_suite()
        assert len(report.results) == 6
        assert "single" in report.summary
        assert "orchestrated" in report.summary

    def test_run_suite_single_mode(self, sample_tasks: list[BenchmarkTask], tmp_path: Path) -> None:
        suite = ComparativeBenchmark(tasks=sample_tasks, workdir=tmp_path)
        report = suite.run_suite(modes=["single"])
        assert len(report.results) == 3
        assert all(r.mode == "single" for r in report.results)

    def test_run_suite_orchestrated_mode(self, sample_tasks: list[BenchmarkTask], tmp_path: Path) -> None:
        suite = ComparativeBenchmark(tasks=sample_tasks, workdir=tmp_path)
        report = suite.run_suite(modes=["orchestrated"])
        assert len(report.results) == 3
        assert all(r.mode == "orchestrated" for r in report.results)


# ---------------------------------------------------------------------------
# TestLoadBundledBenchmarks
# ---------------------------------------------------------------------------


class TestLoadBundledBenchmarks:
    """Tests that the bundled templates/benchmarks/ YAML files load correctly."""

    def test_bundled_tasks_load(self) -> None:
        bundled_dir = Path(__file__).resolve().parents[2] / "templates" / "benchmarks"
        if not bundled_dir.is_dir():
            pytest.skip("templates/benchmarks/ not found (expected in repo root)")

        tasks = load_benchmark_tasks(bundled_dir)
        assert len(tasks) >= 5

        ids = {t.task_id for t in tasks}
        assert "fix-off-by-one" in ids
        assert "add-unit-tests" in ids
        assert "extract-function" in ids
        assert "add-docstrings" in ids
        assert "fix-import-error" in ids

    def test_bundled_task_types(self) -> None:
        bundled_dir = Path(__file__).resolve().parents[2] / "templates" / "benchmarks"
        if not bundled_dir.is_dir():
            pytest.skip("templates/benchmarks/ not found (expected in repo root)")

        tasks = load_benchmark_tasks(bundled_dir)
        types = {t.task_type for t in tasks}
        assert types == {"bugfix", "test", "refactor", "docs"}
