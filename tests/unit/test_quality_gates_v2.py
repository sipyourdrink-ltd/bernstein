"""Regression tests for quality-gates-v2 additive behavior."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bernstein.core.gate_runner import GatePipelineStep, GateReport, GateResult, build_default_pipeline
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.quality_gates import QualityGatesConfig, run_quality_gates


def _task() -> Task:
    return Task(
        id="T-v2-1",
        title="Quality gates v2",
        description="Exercise additive gates.",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
    )


def test_default_pipeline_includes_additive_gates() -> None:
    config = QualityGatesConfig(
        lint=True,
        type_check=True,
        tests=True,
        security_scan=True,
        complexity_check=True,
        dead_code_check=True,
        import_cycle_check=True,
        coverage_delta=True,
        merge_conflict_check=True,
        pii_scan=True,
    )

    pipeline = build_default_pipeline(config)

    assert [step.name for step in pipeline] == [
        "lint",
        "type_check",
        "tests",
        "security_scan",
        "complexity_check",
        "dead_code",
        "import_cycle",
        "coverage_delta",
        "merge_conflict",
        "pii_scan",
        "dlp_scan",
    ]


def test_run_quality_gates_preserves_warn_status_and_records_quality_score(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        enabled=True,
        pipeline=[GatePipelineStep(name="tests", required=True, condition="always")],
    )
    report = GateReport(
        task_id="T-v2-1",
        overall_pass=True,
        total_duration_ms=12,
        gates_run=["tests"],
        results=[
            GateResult(
                name="tests",
                status="warn",
                required=True,
                blocked=False,
                cached=False,
                duration_ms=12,
                details="all tests passing; newly detected flaky tests: tests/unit/test_demo.py::test_flaky",
                metadata={"new_flaky_tests": ["tests/unit/test_demo.py::test_flaky"]},
            )
        ],
        changed_files=["src/demo.py"],
        cache_hits=0,
    )

    with patch("bernstein.core.gate_runner.GateRunner.run_all", new=AsyncMock(return_value=report)):
        result = run_quality_gates(_task(), tmp_path, tmp_path, config)

    assert result.passed is True
    assert result.gate_results[0].status == "warn"
    assert result.quality_score is not None
    assert result.quality_score.total == 50

    metrics_path = tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl"
    event = json.loads(metrics_path.read_text(encoding="utf-8").strip())
    assert event["result"] == "flagged"
    assert event["status"] == "warn"
    assert event["new_flaky_tests"] == ["tests/unit/test_demo.py::test_flaky"]


def test_warn_timeout_and_bypassed_map_to_legacy_flagged_metrics(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        enabled=True,
        pipeline=[
            GatePipelineStep(name="lint", required=True, condition="always"),
            GatePipelineStep(name="tests", required=True, condition="always"),
            GatePipelineStep(name="dead_code", required=False, condition="always"),
        ],
    )
    report = GateReport(
        task_id="T-v2-1",
        overall_pass=True,
        total_duration_ms=30,
        gates_run=["lint", "tests", "dead_code"],
        results=[
            GateResult("lint", "warn", True, False, False, 10, "warn"),
            GateResult("tests", "timeout", True, False, False, 10, "timeout"),
            GateResult("dead_code", "bypassed", False, False, False, 0, "manual"),
        ],
        changed_files=["src/demo.py"],
        cache_hits=0,
    )

    with patch("bernstein.core.gate_runner.GateRunner.run_all", new=AsyncMock(return_value=report)):
        run_quality_gates(_task(), tmp_path, tmp_path, config)

    metrics_path = tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl"
    events = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]

    assert [event["result"] for event in events] == ["flagged", "flagged", "flagged"]
    assert [event["status"] for event in events] == ["warn", "timeout", "bypassed"]


def test_skipped_status_maps_to_pass_metric(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        enabled=True,
        pipeline=[GatePipelineStep(name="lint", required=True, condition="always")],
    )
    report = GateReport(
        task_id="T-v2-1",
        overall_pass=True,
        total_duration_ms=5,
        gates_run=["lint"],
        results=[GateResult("lint", "skipped", True, False, False, 0, "No files changed")],
        changed_files=[],
        cache_hits=0,
    )

    with patch("bernstein.core.gate_runner.GateRunner.run_all", new=AsyncMock(return_value=report)):
        result = run_quality_gates(_task(), tmp_path, tmp_path, config)

    assert result.gate_results[0].status == "skipped"
    event = json.loads((tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl").read_text(encoding="utf-8").strip())
    assert event["result"] == "pass"


def test_blocked_gate_maps_to_blocked_metric(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        enabled=True,
        pipeline=[GatePipelineStep(name="lint", required=True, condition="always")],
    )
    report = GateReport(
        task_id="T-v2-1",
        overall_pass=False,
        total_duration_ms=5,
        gates_run=["lint"],
        results=[GateResult("lint", "fail", True, True, False, 1, "lint error")],
        changed_files=["src/demo.py"],
        cache_hits=0,
    )

    with patch("bernstein.core.gate_runner.GateRunner.run_all", new=AsyncMock(return_value=report)):
        result = run_quality_gates(_task(), tmp_path, tmp_path, config)

    assert result.passed is False
    event = json.loads((tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl").read_text(encoding="utf-8").strip())
    assert event["result"] == "blocked"


def test_quality_score_failure_is_best_effort(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        enabled=True,
        pipeline=[GatePipelineStep(name="lint", required=True, condition="always")],
    )
    report = GateReport(
        task_id="T-v2-1",
        overall_pass=True,
        total_duration_ms=5,
        gates_run=["lint"],
        results=[GateResult("lint", "pass", True, False, False, 1, "ok")],
        changed_files=["src/demo.py"],
        cache_hits=0,
    )

    with (
        patch("bernstein.core.gate_runner.GateRunner.run_all", new=AsyncMock(return_value=report)),
        patch("bernstein.core.quality_score.QualityScorer.score", side_effect=RuntimeError("boom")),
    ):
        result = run_quality_gates(_task(), tmp_path, tmp_path, config)

    assert result.passed is True
    assert result.quality_score is None
