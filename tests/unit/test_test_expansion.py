"""Unit tests for bernstein.core.test_expansion — regression test auto-expansion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.test_expansion import (
    NeedsCoverageRecord,
    ExpansionResult,
    find_uncovered_source_files,
    write_needs_coverage,
)


# ---------------------------------------------------------------------------
# find_uncovered_source_files
# ---------------------------------------------------------------------------


def test_source_file_with_test_is_covered(tmp_path: Path) -> None:
    """A source file that has a matching test_*.py is not flagged."""
    src = tmp_path / "src" / "bernstein" / "core"
    src.mkdir(parents=True)
    (src / "my_module.py").write_text("def foo(): pass\n", encoding="utf-8")

    tests = tmp_path / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_my_module.py").write_text("def test_foo(): pass\n", encoding="utf-8")

    result = find_uncovered_source_files(
        changed_files=["src/bernstein/core/my_module.py"],
        workdir=tmp_path,
    )
    assert result.uncovered_files == []
    assert result.covered_files == ["src/bernstein/core/my_module.py"]


def test_source_file_without_test_is_uncovered(tmp_path: Path) -> None:
    """A source file with no matching test file is flagged as uncovered."""
    src = tmp_path / "src" / "bernstein" / "core"
    src.mkdir(parents=True)
    (src / "new_feature.py").write_text("def bar(): pass\n", encoding="utf-8")

    result = find_uncovered_source_files(
        changed_files=["src/bernstein/core/new_feature.py"],
        workdir=tmp_path,
    )
    assert "src/bernstein/core/new_feature.py" in result.uncovered_files
    assert result.covered_files == []


def test_test_files_themselves_are_skipped(tmp_path: Path) -> None:
    """Changed test files are not checked for test coverage — they ARE tests."""
    result = find_uncovered_source_files(
        changed_files=["tests/unit/test_foo.py", "tests/integration/test_bar.py"],
        workdir=tmp_path,
    )
    assert result.uncovered_files == []
    assert result.covered_files == []


def test_non_python_files_are_skipped(tmp_path: Path) -> None:
    """Non-.py files (YAML, Markdown, config) are not checked."""
    result = find_uncovered_source_files(
        changed_files=["pyproject.toml", "README.md", "scripts/build.sh"],
        workdir=tmp_path,
    )
    assert result.uncovered_files == []


def test_init_files_are_skipped(tmp_path: Path) -> None:
    """__init__.py files carry no unique logic to test."""
    result = find_uncovered_source_files(
        changed_files=["src/bernstein/__init__.py", "src/bernstein/core/__init__.py"],
        workdir=tmp_path,
    )
    assert result.uncovered_files == []


def test_mixed_changed_files(tmp_path: Path) -> None:
    """Mix of covered, uncovered, test, and non-py files resolves correctly."""
    src = tmp_path / "src" / "bernstein" / "core"
    src.mkdir(parents=True)
    (src / "covered.py").write_text("x = 1\n", encoding="utf-8")
    (src / "uncovered.py").write_text("y = 2\n", encoding="utf-8")

    tests_dir = tmp_path / "tests" / "unit"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_covered.py").write_text("def test_x(): pass\n", encoding="utf-8")

    result = find_uncovered_source_files(
        changed_files=[
            "src/bernstein/core/covered.py",
            "src/bernstein/core/uncovered.py",
            "tests/unit/test_covered.py",
            "pyproject.toml",
        ],
        workdir=tmp_path,
    )
    assert result.covered_files == ["src/bernstein/core/covered.py"]
    assert result.uncovered_files == ["src/bernstein/core/uncovered.py"]


# ---------------------------------------------------------------------------
# write_needs_coverage
# ---------------------------------------------------------------------------


def test_write_needs_coverage_creates_file(tmp_path: Path) -> None:
    """write_needs_coverage writes a JSON file listing uncovered source files."""
    records = [
        NeedsCoverageRecord(source_file="src/bernstein/core/new_thing.py", task_id="T-001"),
        NeedsCoverageRecord(source_file="src/bernstein/core/other.py", task_id="T-001"),
    ]
    out_path = write_needs_coverage(records, workdir=tmp_path)

    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["source_file"] == "src/bernstein/core/new_thing.py"
    assert data[0]["task_id"] == "T-001"


def test_write_needs_coverage_merges_with_existing(tmp_path: Path) -> None:
    """write_needs_coverage appends to an existing list without duplicates."""
    existing = [{"source_file": "src/bernstein/core/old.py", "task_id": "T-000"}]
    out_dir = tmp_path / ".sdd" / "runtime"
    out_dir.mkdir(parents=True)
    out_file = out_dir / "needs_coverage.json"
    out_file.write_text(json.dumps(existing), encoding="utf-8")

    records = [
        NeedsCoverageRecord(source_file="src/bernstein/core/new.py", task_id="T-001"),
        # Duplicate should not be added again
        NeedsCoverageRecord(source_file="src/bernstein/core/old.py", task_id="T-002"),
    ]
    write_needs_coverage(records, workdir=tmp_path)

    data = json.loads(out_file.read_text(encoding="utf-8"))
    source_files = [entry["source_file"] for entry in data]
    # old.py already existed — no duplicate
    assert source_files.count("src/bernstein/core/old.py") == 1
    assert "src/bernstein/core/new.py" in source_files


# ---------------------------------------------------------------------------
# ExpansionResult
# ---------------------------------------------------------------------------


def test_expansion_result_needs_action_only_when_uncovered_files_exist() -> None:
    """needs_action is True iff uncovered_files is non-empty."""
    assert ExpansionResult(uncovered_files=[], covered_files=[]).needs_action is False
    assert ExpansionResult(
        uncovered_files=["src/a.py"], covered_files=[]
    ).needs_action is True


# ---------------------------------------------------------------------------
# Gate integration — test_expansion gate in GateRunner
# ---------------------------------------------------------------------------


def test_test_expansion_gate_passes_and_records_uncovered(tmp_path: Path) -> None:
    """test_expansion gate always passes and writes needs_coverage.json for uncovered files."""
    import asyncio

    from bernstein.core.gate_runner import GatePipelineStep, GateRunner
    from bernstein.core.models import Complexity, Scope, Task
    from bernstein.core.quality_gates import QualityGatesConfig

    src = tmp_path / "src" / "bernstein" / "core"
    src.mkdir(parents=True)
    (src / "new_module.py").write_text("def foo(): pass\n", encoding="utf-8")

    config = QualityGatesConfig(
        test_expansion=True,
        pipeline=[GatePipelineStep(name="test_expansion", required=False, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = Task(
        id="T-exp-1",
        title="Test expansion task",
        description="",
        role="backend",
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        owned_files=["src/bernstein/core/new_module.py"],
    )

    report = asyncio.run(runner.run_all(task, tmp_path))
    result = report.results[0]

    assert result.name == "test_expansion"
    assert result.status == "pass"
    assert result.blocked is False

    out_file = tmp_path / ".sdd" / "runtime" / "needs_coverage.json"
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    source_files = [e["source_file"] for e in data]
    assert "src/bernstein/core/new_module.py" in source_files


def test_test_expansion_gate_skips_when_all_files_have_tests(tmp_path: Path) -> None:
    """test_expansion gate skips (no output file written) when all changed files are covered."""
    import asyncio

    from bernstein.core.gate_runner import GatePipelineStep, GateRunner
    from bernstein.core.models import Complexity, Scope, Task
    from bernstein.core.quality_gates import QualityGatesConfig

    src = tmp_path / "src" / "bernstein" / "core"
    src.mkdir(parents=True)
    (src / "covered.py").write_text("def foo(): pass\n", encoding="utf-8")
    tests_dir = tmp_path / "tests" / "unit"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_covered.py").write_text("def test_foo(): pass\n", encoding="utf-8")

    config = QualityGatesConfig(
        test_expansion=True,
        pipeline=[GatePipelineStep(name="test_expansion", required=False, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = Task(
        id="T-exp-2",
        title="Covered task",
        description="",
        role="backend",
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        owned_files=["src/bernstein/core/covered.py"],
    )

    report = asyncio.run(runner.run_all(task, tmp_path))
    result = report.results[0]
    assert result.status == "pass"
    assert result.blocked is False
    assert "already covered" in result.details or "all" in result.details.lower()


def test_test_expansion_in_default_pipeline(tmp_path: Path) -> None:
    """test_expansion appears in the default pipeline when enabled."""
    from bernstein.core.gate_runner import build_default_pipeline
    from bernstein.core.quality_gates import QualityGatesConfig

    config = QualityGatesConfig(test_expansion=True)
    pipeline = build_default_pipeline(config)
    names = [step.name for step in pipeline]
    assert "test_expansion" in names
