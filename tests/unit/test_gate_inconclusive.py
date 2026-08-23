"""Tests for the inconclusive gate verdict (issue #4181).

Covers:
1. GateResult status<->reason invariant (inconclusive must carry a closed-set
   reason; every other status must carry reason=None).
2. Blocking semantics: at a required gate, inconclusive blocks exactly like
   fail (the verdict differs, the outcome does not).
3. No-bypass regression: no path maps inconclusive to pass (legacy result
   conversion and score aggregation).
4. Producer path: an exception inside a gate evaluator yields
   inconclusive + runner-died-before-output instead of fail.
5. Consumer path: _points_for_status gives inconclusive its own band.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.quality.gate_pipeline import (
    INCONCLUSIVE_REASONS,
    GatePipelineStep,
    GateReport,
    GateResult,
)
from bernstein.core.quality.gate_runner import GateRunner
from bernstein.core.quality.quality_gates import (
    QualityGatesConfig,
    _legacy_result_from_report,
)
from bernstein.core.quality.quality_score import QualityScorer


def _result(status: str, *, reason: str | None = None, blocked: bool = False) -> GateResult:
    return GateResult(
        name="lint",
        status=status,  # type: ignore[arg-type]
        required=True,
        blocked=blocked,
        cached=False,
        duration_ms=10,
        details="test",
        metadata={},
        reason=reason,
    )


class TestGateResultInvariant:
    def test_inconclusive_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="requires a reason"):
            _result("inconclusive", reason=None)

    def test_inconclusive_reason_must_be_closed_set(self) -> None:
        with pytest.raises(ValueError, match="not in the closed set"):
            _result("inconclusive", reason="made-up-reason")

    def test_inconclusive_accepts_every_closed_reason(self) -> None:
        for code in INCONCLUSIVE_REASONS:
            r = _result("inconclusive", reason=code, blocked=True)
            assert r.status == "inconclusive"
            assert r.reason == code

    def test_non_inconclusive_must_not_carry_reason(self) -> None:
        with pytest.raises(ValueError, match="only valid with"):
            _result("fail", reason="runner-died-before-output")

    def test_plain_verdicts_accept_reason_none(self) -> None:
        for status in ("pass", "fail", "warn", "timeout", "skipped", "bypassed"):
            r = _result(status, reason=None)
            assert r.status == status
            assert r.reason is None


class TestBlockingSemantics:
    def test_inconclusive_blocks_at_required_gate(self) -> None:
        # Mirrors the producer sites: blocked is set to step.required,
        # exactly like fail.
        step = GatePipelineStep(name="lint", required=True, condition="python_changed")
        r = GateResult(
            name=step.name,
            status="inconclusive",
            required=step.required,
            blocked=step.required,
            cached=False,
            duration_ms=0,
            details="x",
            metadata={},
            reason="runner-died-before-output",
        )
        assert r.blocked is True

    def test_inconclusive_does_not_block_optional_gate(self) -> None:
        step = GatePipelineStep(name="lint", required=False, condition="python_changed")
        r = GateResult(
            name=step.name,
            status="inconclusive",
            required=step.required,
            blocked=step.required,
            cached=False,
            duration_ms=0,
            details="x",
            metadata={},
            reason="runner-died-before-output",
        )
        assert r.blocked is False


class TestNoBypassRegression:
    def test_inconclusive_never_maps_to_passed(self) -> None:
        report = GateReport(
            task_id="T-inconclusive",
            overall_pass=False,
            total_duration_ms=10,
            gates_run=["lint"],
            results=[_result("inconclusive", reason="evidence-missing", blocked=True)],
            changed_files=[],
            cache_hits=0,
        )
        legacy = _legacy_result_from_report(report)
        assert len(legacy.gate_results) == 1
        assert legacy.gate_results[0].passed is False
        assert legacy.gate_results[0].status == "inconclusive"

    def test_inconclusive_reports_blocked(self) -> None:
        report = GateReport(
            task_id="T-inconclusive",
            overall_pass=False,
            total_duration_ms=10,
            gates_run=["lint"],
            results=[_result("inconclusive", reason="evidence-missing", blocked=True)],
            changed_files=[],
            cache_hits=0,
        )
        legacy = _legacy_result_from_report(report)
        assert legacy.gate_results[0].blocked is True


class TestProducerPath:
    def test_plugin_exception_becomes_inconclusive(self, tmp_path: Path) -> None:
        def boom(_files, _run_dir, _title, _description):
            raise RuntimeError("plugin crashed")

        config = QualityGatesConfig(
            pipeline=[
                GatePipelineStep(name="custom_gate", required=True, condition="python_changed"),
            ],
            cache_enabled=False,
        )
        runner = GateRunner(config, tmp_path)
        # Register a fake plugin that dies before producing output.
        runner._plugin_registry = lambda: type(  # type: ignore[method-assign]
            "FakeRegistry",
            (),
            {"get": lambda self, _name: type("P", (), {"run": staticmethod(boom)})()},
        )()
        # Use the sync dispatch path via plugin gate.
        import asyncio

        from bernstein.core.models import Complexity, Scope, Task

        task = Task(
            id="T-plugin-boom",
            title="t",
            description="d",
            role="backend",
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            owned_files=["src/a.py"],
        )
        result = asyncio.run(
            runner._execute_plugin_gate(
                GatePipelineStep(name="custom_gate", required=True, condition="python_changed"),
                task,
                tmp_path,
                ["src/a.py"],
            )
        )
        assert result.status == "inconclusive"
        assert result.reason == "runner-died-before-output"
        assert result.blocked is True

    def test_complexity_gate_missing_baseline_is_inconclusive(self, tmp_path: Path, monkeypatch) -> None:
        """Missing baseline evidence is inconclusive, not a non-blocking warn.

        Review (chernistry, PR #4282): the complexity gate's baseline
        unavailable branch returned ``warn``/``blocked=False`` — a silent
        downgrade that reads as "seen and non-blocking" rather than "not
        evaluated". Issue #4181 opens with exactly this absent-evidence
        shape. ``evidence-missing`` + blocked=required is the honest verdict.
        """
        config = QualityGatesConfig(
            pipeline=[
                GatePipelineStep(name="complexity_check", required=True, condition="python_changed"),
            ],
            complexity_check_command="radon cc",
            cache_enabled=False,
        )
        runner = GateRunner(config, tmp_path)
        monkeypatch.setattr(runner, "_measure_complexity_sync", lambda _cmd, _run_dir: (12.5, "ok"))
        monkeypatch.setattr(runner, "_measure_complexity_base_sync", lambda _cmd: (None, "no baseline file"))
        result = runner._run_complexity_gate_sync(
            GatePipelineStep(name="complexity_check", required=True, condition="python_changed"),
            tmp_path,
            ["src/a.py"],
        )
        assert result.status == "inconclusive"
        assert result.reason == "evidence-missing"
        assert result.blocked is True
        assert "baseline unavailable" in result.details

    def test_complexity_gate_missing_baseline_optional_gate(self, tmp_path: Path, monkeypatch) -> None:
        """At an optional gate, inconclusive does not block."""
        config = QualityGatesConfig(
            pipeline=[
                GatePipelineStep(name="complexity_check", required=False, condition="python_changed"),
            ],
            complexity_check_command="radon cc",
            cache_enabled=False,
        )
        runner = GateRunner(config, tmp_path)
        monkeypatch.setattr(runner, "_measure_complexity_sync", lambda _cmd, _run_dir: (12.5, "ok"))
        monkeypatch.setattr(runner, "_measure_complexity_base_sync", lambda _cmd: (None, "no baseline file"))
        result = runner._run_complexity_gate_sync(
            GatePipelineStep(name="complexity_check", required=False, condition="python_changed"),
            tmp_path,
            ["src/a.py"],
        )
        assert result.status == "inconclusive"
        assert result.reason == "evidence-missing"
        assert result.blocked is False

    def test_command_could_not_run_is_inconclusive(self, tmp_path: Path) -> None:
        """OSError from subprocess.run (tool could not start) is absent
        evidence: inconclusive + evidence-missing, not fail."""
        config = QualityGatesConfig(cache_enabled=False)
        runner = GateRunner(config, tmp_path)
        step = GatePipelineStep(name="lint", required=True, condition="python_changed")
        result = runner._command_failure_result(
            step, "Command error: [Errno 2] No such file or directory", "ruff check"
        )
        assert result.status == "inconclusive"
        assert result.reason == "evidence-missing"
        assert result.blocked is True

    def test_command_real_failure_stays_fail(self, tmp_path: Path) -> None:
        """Non-zero exit with captured output is a real (unfavourable)
        verdict from a tool that ran — fail, not inconclusive."""
        config = QualityGatesConfig(cache_enabled=False)
        runner = GateRunner(config, tmp_path)
        step = GatePipelineStep(name="lint", required=True, condition="python_changed")
        result = runner._command_failure_result(step, "src/a.py:1:1 E501 line too long (89 > 88)", "ruff check")
        assert result.status == "fail"
        assert result.reason is None
        assert result.blocked is True

    def test_command_timeout_stays_timeout(self, tmp_path: Path) -> None:
        """Timeout keeps its own verdict — unchanged by the inconclusive work."""
        config = QualityGatesConfig(cache_enabled=False)
        runner = GateRunner(config, tmp_path)
        step = GatePipelineStep(name="lint", required=True, condition="python_changed")
        result = runner._command_failure_result(step, "Timed out after 120s", "ruff check")
        assert result.status == "timeout"
        assert result.reason is None
        assert result.blocked is True


class TestConsumerScore:
    def test_points_for_status_bands(self, tmp_path: Path) -> None:
        scorer = QualityScorer(tmp_path)
        assert scorer._points_for_status("pass") == 100
        assert scorer._points_for_status("warn") == 50
        assert scorer._points_for_status("timeout") == 50
        assert scorer._points_for_status("inconclusive") == 30
        assert scorer._points_for_status("fail") == 0
        assert scorer._points_for_status("skipped") == 0
        assert scorer._points_for_status("bypassed") == 0
