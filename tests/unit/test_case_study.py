"""Tests for case study generator (road-017)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.case_study import (
    CaseStudyConfig,
    export_case_study,
    generate_case_study,
)


def _setup_sdd(run_dir: Path, summary: dict | None = None, tasks: int = 3) -> None:
    """Create mock .sdd/ data for a run directory."""
    sdd = run_dir / ".sdd"
    sdd.mkdir(parents=True)
    metrics = sdd / "metrics"
    metrics.mkdir()

    if summary is None:
        summary = {
            "goal": "Build the feature",
            "total_tasks": tasks,
            "completed_tasks": tasks - 1,
            "failed_tasks": 1,
            "total_cost_usd": 0.42,
            "duration_s": 300.0,
        }
    (sdd / "summary.json").write_text(json.dumps(summary))

    for i in range(tasks):
        data = {"role": "backend" if i % 2 == 0 else "qa", "model": "sonnet"}
        (metrics / f"task-{i}.json").write_text(json.dumps(data))


def test_generate_basic(tmp_path: Path) -> None:
    """generate_case_study produces a Markdown document."""
    _setup_sdd(tmp_path)
    config = CaseStudyConfig(title="Test Run", author="Tester")
    result = generate_case_study(tmp_path, config)

    assert "# Test Run" in result
    assert "Author: Tester" in result
    assert "Executive Summary" in result
    assert "Problem Statement" in result
    assert "Approach" in result
    assert "Results" in result
    assert "Lessons Learned" in result


def test_generate_contains_stats(tmp_path: Path) -> None:
    """generate_case_study includes task counts and cost."""
    _setup_sdd(tmp_path)
    config = CaseStudyConfig()
    result = generate_case_study(tmp_path, config)

    assert "3" in result  # total tasks
    assert "$0.42" in result  # total cost
    assert "5.0m" in result  # 300s formatted


def test_generate_no_costs(tmp_path: Path) -> None:
    """generate_case_study omits costs when include_costs is False."""
    _setup_sdd(tmp_path)
    config = CaseStudyConfig(include_costs=False)
    result = generate_case_study(tmp_path, config)

    assert "$0.42" not in result


def test_generate_no_timeline(tmp_path: Path) -> None:
    """generate_case_study omits timeline when include_timeline is False."""
    _setup_sdd(tmp_path)
    config = CaseStudyConfig(include_timeline=False)
    result = generate_case_study(tmp_path, config)

    assert "Total duration" not in result


def test_generate_empty_sdd(tmp_path: Path) -> None:
    """generate_case_study handles missing .sdd/ gracefully."""
    config = CaseStudyConfig(title="Empty")
    result = generate_case_study(tmp_path, config)
    assert "# Empty" in result
    assert "Executive Summary" in result


def test_generate_agents_and_models(tmp_path: Path) -> None:
    """generate_case_study lists agents and models used."""
    _setup_sdd(tmp_path, tasks=4)
    config = CaseStudyConfig()
    result = generate_case_study(tmp_path, config)

    assert "backend" in result
    assert "qa" in result
    assert "sonnet" in result


def test_export_case_study(tmp_path: Path) -> None:
    """export_case_study writes content to a file."""
    out = tmp_path / "output" / "study.md"
    result = export_case_study("# Hello\nWorld", out)
    assert result == out
    assert out.read_text() == "# Hello\nWorld"


def test_export_case_study_creates_parents(tmp_path: Path) -> None:
    """export_case_study creates parent directories if needed."""
    out = tmp_path / "deep" / "nested" / "study.md"
    export_case_study("content", out)
    assert out.exists()
