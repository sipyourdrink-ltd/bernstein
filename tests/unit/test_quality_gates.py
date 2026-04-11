"""Tests for automated quality gates: lint, type-check, test, and intent verification gates."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.gate_runner import GatePipelineStep
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.quality_gates import (
    IntentVerdict,
    IntentVerificationConfig,
    QualityGatesConfig,
    _get_intent_diff,
    _parse_intent_response,
    _run_command,
    get_quality_gate_stats,
    run_quality_gates,
)


def _make_task(*, id: str = "T-001", role: str = "backend") -> Task:
    return Task(
        id=id,
        title="Test task",
        description="Do something.",
        role=role,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
    )


# ---------------------------------------------------------------------------
# _run_command
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_success_exit_zero(self, tmp_path: Path) -> None:
        ok, output = _run_command("exit 0", tmp_path, timeout_s=5)
        assert ok
        assert isinstance(output, str)

    def test_failure_nonzero_exit(self, tmp_path: Path) -> None:
        ok, _output = _run_command("exit 1", tmp_path, timeout_s=5)
        assert not ok

    def test_captures_stdout(self, tmp_path: Path) -> None:
        ok, output = _run_command("echo hello_world", tmp_path, timeout_s=5)
        assert ok
        assert "hello_world" in output

    def test_timeout_returns_failure(self, tmp_path: Path) -> None:
        ok, output = _run_command("sleep 10", tmp_path, timeout_s=1)
        assert not ok
        assert "Timed out" in output

    def test_truncates_long_output(self, tmp_path: Path) -> None:
        long_str = "x" * 3000
        ok, output = _run_command(f"echo '{long_str}'", tmp_path, timeout_s=5)
        assert ok
        assert len(output) <= 2050  # 2000 + "... (truncated)"

    def test_bad_command_returns_failure(self, tmp_path: Path) -> None:
        # Command that doesn't exist
        ok, _output = _run_command("nonexistent_command_xyz_12345", tmp_path, timeout_s=5)
        assert not ok


# ---------------------------------------------------------------------------
# run_quality_gates: disabled master switch
# ---------------------------------------------------------------------------


class TestRunQualityGatesDisabled:
    def test_disabled_returns_passed(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(enabled=False)
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        assert result.gate_results == []

    def test_disabled_no_commands_run(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(enabled=False, lint=True)
        task = _make_task()
        with patch("bernstein.core.quality_gates._run_command") as mock_run:
            run_quality_gates(task, tmp_path, tmp_path, config)
            mock_run.assert_not_called()

    def test_bypass_rejected_when_disabled_in_config(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(enabled=True, allow_bypass=False)
        task = _make_task()
        with pytest.raises(ValueError, match="bypass is disabled"):
            run_quality_gates(task, tmp_path, tmp_path, config, skip_gates=["lint"])


# ---------------------------------------------------------------------------
# run_quality_gates: lint gate
# ---------------------------------------------------------------------------


class TestLintGate:
    def test_lint_pass_no_violations(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=True,
            lint_command="exit 0",
            type_check=False,
            tests=False,
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        assert len(result.gate_results) == 1
        assert result.gate_results[0].gate == "lint"
        assert result.gate_results[0].passed
        assert not result.gate_results[0].blocked

    def test_lint_fail_blocks_merge(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=True,
            lint_command="exit 1",
            type_check=False,
            tests=False,
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert not result.passed
        assert result.gate_results[0].gate == "lint"
        assert not result.gate_results[0].passed
        assert result.gate_results[0].blocked  # hard block

    def test_lint_skipped_when_disabled(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True, lint=False, type_check=False, tests=False, pii_scan=False, dlp_scan=False
        )
        task = _make_task()
        with patch("bernstein.core.quality_gates._run_command") as mock_run:
            result = run_quality_gates(task, tmp_path, tmp_path, config)
            mock_run.assert_not_called()
        assert result.passed
        assert result.gate_results == []


# ---------------------------------------------------------------------------
# run_quality_gates: type check gate
# ---------------------------------------------------------------------------


class TestTypeCheckGate:
    def test_type_check_pass(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=True,
            type_check_command="exit 0",
            tests=False,
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        assert result.gate_results[0].gate == "type_check"

    def test_type_check_fail_blocks(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=True,
            type_check_command="exit 1",
            tests=False,
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert not result.passed
        assert result.gate_results[0].blocked


# ---------------------------------------------------------------------------
# run_quality_gates: test gate
# ---------------------------------------------------------------------------


class TestTestGate:
    def test_tests_pass(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=False,
            tests=True,
            test_command="exit 0",
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        assert result.gate_results[0].gate == "tests"

    def test_tests_fail_blocks(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=False,
            tests=True,
            test_command="exit 1",
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert not result.passed
        assert result.gate_results[0].blocked


# ---------------------------------------------------------------------------
# run_quality_gates: multiple gates, all run even if first fails
# ---------------------------------------------------------------------------


class TestMultipleGates:
    def test_all_gates_run_even_if_lint_fails(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=True,
            lint_command="exit 1",
            type_check=True,
            type_check_command="exit 0",
            tests=True,
            test_command="exit 0",
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert not result.passed
        assert len(result.gate_results) == 3
        gates = {r.gate: r for r in result.gate_results}
        assert not gates["lint"].passed
        assert gates["type_check"].passed
        assert gates["tests"].passed

    def test_all_pass_returns_passed(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=True,
            lint_command="exit 0",
            type_check=True,
            type_check_command="exit 0",
            tests=True,
            test_command="exit 0",
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        assert all(r.passed for r in result.gate_results)


# ---------------------------------------------------------------------------
# Metrics recording
# ---------------------------------------------------------------------------


class TestMetricsRecording:
    def test_records_pass_event(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=True,
            lint_command="exit 0",
            type_check=False,
            tests=False,
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task(id="T-metrics-1")
        run_quality_gates(task, tmp_path, tmp_path, config)

        metrics_file = tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl"
        assert metrics_file.exists()
        line = json.loads(metrics_file.read_text().strip())
        assert line["task_id"] == "T-metrics-1"
        assert line["gate"] == "lint"
        assert line["result"] == "pass"

    def test_records_blocked_event(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=True,
            lint_command="exit 1",
            type_check=False,
            tests=False,
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task(id="T-metrics-2")
        run_quality_gates(task, tmp_path, tmp_path, config)

        metrics_file = tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl"
        line = json.loads(metrics_file.read_text().strip())
        assert line["result"] == "blocked"

    def test_records_rich_gate_fields(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=True,
            lint_command="exit 0",
            type_check=False,
            tests=False,
            pii_scan=False,
            dlp_scan=False,
            cache_enabled=False,
        )
        task = _make_task(id="T-metrics-rich")
        run_quality_gates(task, tmp_path, tmp_path, config)

        metrics_file = tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl"
        line = json.loads(metrics_file.read_text().strip())
        assert line["status"] == "pass"
        assert isinstance(line["duration_ms"], int)
        assert line["cached"] is False
        assert line["required"] is True

    def test_records_bypass_metadata(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            allow_bypass=True,
            pipeline=[GatePipelineStep(name="lint", required=True, condition="always")],
            cache_enabled=False,
        )
        task = _make_task(id="T-bypass")
        run_quality_gates(
            task,
            tmp_path,
            tmp_path,
            config,
            skip_gates=["lint"],
            bypass_reason="operator override",
        )

        metrics_file = tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl"
        line = json.loads(metrics_file.read_text().strip())
        assert line["status"] == "bypassed"
        assert line["result"] == "flagged"
        assert line["reason"] == "operator override"
        assert line["actor"] == "cli"

    def test_gate_execution_creates_telemetry_span(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(
            enabled=True,
            lint=True,
            lint_command="exit 0",
            type_check=False,
            tests=False,
            pii_scan=False,
            dlp_scan=False,
        )
        task = _make_task(id="T-span")
        fake_span = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.__enter__.return_value = fake_span
        fake_ctx.__exit__.return_value = None

        with patch("bernstein.core.gate_runner.start_span", return_value=fake_ctx) as mock_start_span:
            result = run_quality_gates(task, tmp_path, tmp_path, config)

        assert result.passed
        mock_start_span.assert_called_once()
        attrs = mock_start_span.call_args.args[1]
        assert attrs["task.id"] == "T-span"
        assert attrs["quality_gate.name"] == "lint"
        fake_span.set_attribute.assert_any_call("quality_gate.status", "pass")
        fake_span.set_attribute.assert_any_call("quality_gate.blocked", False)

    def test_get_quality_gate_stats_empty(self, tmp_path: Path) -> None:
        stats = get_quality_gate_stats(tmp_path)
        assert stats == {"total": 0, "blocked": 0, "by_gate": {}}

    def test_get_quality_gate_stats_counts(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        events = [
            {"timestamp": "2026-01-01T00:00:00+00:00", "task_id": "T1", "gate": "lint", "result": "pass"},
            {"timestamp": "2026-01-01T00:01:00+00:00", "task_id": "T2", "gate": "lint", "result": "blocked"},
            {"timestamp": "2026-01-01T00:02:00+00:00", "task_id": "T3", "gate": "tests", "result": "pass"},
        ]
        with open(metrics_dir / "quality_gates.jsonl", "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        stats = get_quality_gate_stats(tmp_path)
        assert stats["total"] == 3
        assert stats["blocked"] == 1
        assert stats["by_gate"]["lint"]["pass"] == 1
        assert stats["by_gate"]["lint"]["blocked"] == 1
        assert stats["by_gate"]["tests"]["pass"] == 1


# ---------------------------------------------------------------------------
# Seed config parsing
# ---------------------------------------------------------------------------


class TestSeedQualityGatesParsing:
    def test_parse_quality_gates_defaults(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text(
            "goal: test\nquality_gates:\n  enabled: true\n  lint: true\n",
            encoding="utf-8",
        )
        cfg = parse_seed(seed_file)
        assert cfg.quality_gates is not None
        assert cfg.quality_gates.enabled
        assert cfg.quality_gates.lint
        assert not cfg.quality_gates.type_check
        assert not cfg.quality_gates.tests

    def test_parse_quality_gates_custom_commands(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text(
            "goal: test\nquality_gates:\n  lint_command: 'flake8 .'\n  tests: true\n  test_command: 'pytest'\n",
            encoding="utf-8",
        )
        cfg = parse_seed(seed_file)
        assert cfg.quality_gates is not None
        assert cfg.quality_gates.lint_command == "flake8 ."
        assert cfg.quality_gates.tests
        assert cfg.quality_gates.test_command == "pytest"

    def test_parse_quality_gates_pipeline_and_bypass(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text(
            (
                "goal: test\n"
                "quality_gates:\n"
                "  allow_bypass: true\n"
                "  cache_enabled: false\n"
                "  base_ref: develop\n"
                "  pipeline:\n"
                "    - name: lint\n"
                "      required: true\n"
                "      condition: always\n"
                "    - name: pii_scan\n"
                "      required: false\n"
                "      condition: changed_files.any('.py')\n"
            ),
            encoding="utf-8",
        )
        cfg = parse_seed(seed_file)
        assert cfg.quality_gates is not None
        assert cfg.quality_gates.allow_bypass
        assert not cfg.quality_gates.cache_enabled
        assert cfg.quality_gates.base_ref == "develop"
        assert cfg.quality_gates.pipeline is not None
        assert cfg.quality_gates.pipeline[1].condition == "python_changed"

    def test_parse_no_quality_gates_returns_none(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text("goal: test\n", encoding="utf-8")
        cfg = parse_seed(seed_file)
        assert cfg.quality_gates is None

    def test_parse_quality_gates_invalid_type_raises(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text("goal: test\nquality_gates: not_a_dict\n", encoding="utf-8")
        with pytest.raises(SeedError, match="quality_gates must be a mapping"):
            parse_seed(seed_file)

    def test_parse_quality_gates_invalid_pipeline_condition_raises(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text(
            (
                "goal: test\n"
                "quality_gates:\n"
                "  pipeline:\n"
                "    - name: lint\n"
                "      required: true\n"
                "      condition: impossible\n"
            ),
            encoding="utf-8",
        )
        with pytest.raises(SeedError, match="Unsupported gate condition"):
            parse_seed(seed_file)


# ---------------------------------------------------------------------------
# Intent verification: _parse_intent_response
# ---------------------------------------------------------------------------


class TestParseIntentResponse:
    def test_parses_yes_verdict(self) -> None:
        raw = '{"verdict": "yes", "reason": "Matches intent."}'
        result = _parse_intent_response(raw, "test-model")
        assert result.verdict == "yes"
        assert result.reason == "Matches intent."
        assert result.model == "test-model"

    def test_parses_no_verdict(self) -> None:
        raw = '{"verdict": "no", "reason": "Wrong file changed."}'
        result = _parse_intent_response(raw, "m")
        assert result.verdict == "no"
        assert result.reason == "Wrong file changed."

    def test_parses_partially_verdict(self) -> None:
        raw = '{"verdict": "partially", "reason": "Missing one requirement."}'
        result = _parse_intent_response(raw, "m")
        assert result.verdict == "partially"

    def test_strips_markdown_fences(self) -> None:
        raw = '```json\n{"verdict": "yes", "reason": "ok"}\n```'
        result = _parse_intent_response(raw, "m")
        assert result.verdict == "yes"

    def test_extracts_json_from_prose(self) -> None:
        raw = 'Some preamble {"verdict": "no", "reason": "Nope"} trailing text'
        result = _parse_intent_response(raw, "m")
        assert result.verdict == "no"

    def test_unparseable_defaults_to_yes(self) -> None:
        result = _parse_intent_response("not json at all", "m")
        assert result.verdict == "yes"
        assert "defaulting to yes" in result.reason

    def test_unknown_verdict_defaults_to_yes(self) -> None:
        raw = '{"verdict": "maybe", "reason": "who knows"}'
        result = _parse_intent_response(raw, "m")
        assert result.verdict == "yes"


# ---------------------------------------------------------------------------
# Intent verification: _get_intent_diff
# ---------------------------------------------------------------------------


class TestGetIntentDiff:
    def test_returns_string_on_subprocess_failure(self, tmp_path: Path) -> None:
        # Not a git repo — subprocess will fail
        diff = _get_intent_diff(tmp_path, [])
        assert isinstance(diff, str)
        assert len(diff) > 0


# ---------------------------------------------------------------------------
# Intent verification: run_quality_gates integration
# ---------------------------------------------------------------------------


class TestIntentVerificationGate:
    def _make_task_with_summary(self, *, summary: str | None = "Added the feature.") -> Task:
        return Task(
            id="T-intent-1",
            title="Add login feature",
            description="Implement user login with email and password.",
            role="backend",
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            result_summary=summary,
        )

    def test_disabled_by_default(self, tmp_path: Path) -> None:
        """Intent verification gate is off by default — no LLM calls made."""
        config = QualityGatesConfig(enabled=True, lint=False, type_check=False, tests=False)
        task = self._make_task_with_summary()
        with patch("bernstein.core.quality_gates._run_intent_gate") as mock_gate:
            run_quality_gates(task, tmp_path, tmp_path, config)
            mock_gate.assert_not_called()

    def test_enabled_yes_verdict_passes(self, tmp_path: Path) -> None:
        """verdict=yes → gate passes, not blocked."""
        iv_cfg = IntentVerificationConfig(enabled=True)
        config = QualityGatesConfig(enabled=True, lint=False, type_check=False, tests=False, intent_verification=iv_cfg)
        task = self._make_task_with_summary()
        mock_verdict = IntentVerdict(verdict="yes", reason="Matches.", model="test-model")
        with patch("bernstein.core.quality_gates._run_intent_gate", return_value=(mock_verdict, False)):
            result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        iv_result = next(r for r in result.gate_results if r.gate == "intent_verification")
        assert iv_result.passed
        assert not iv_result.blocked

    def test_enabled_no_verdict_blocks(self, tmp_path: Path) -> None:
        """verdict=no with block_on_no=True → gate blocks."""
        iv_cfg = IntentVerificationConfig(enabled=True, block_on_no=True)
        config = QualityGatesConfig(enabled=True, lint=False, type_check=False, tests=False, intent_verification=iv_cfg)
        task = self._make_task_with_summary()
        mock_verdict = IntentVerdict(verdict="no", reason="Wrong thing.", model="test-model")
        with patch("bernstein.core.quality_gates._run_intent_gate", return_value=(mock_verdict, True)):
            result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert not result.passed
        iv_result = next(r for r in result.gate_results if r.gate == "intent_verification")
        assert iv_result.blocked

    def test_enabled_partial_verdict_passes_when_not_blocking(self, tmp_path: Path) -> None:
        """verdict=partially with block_on_partial=False → passes (warn only)."""
        iv_cfg = IntentVerificationConfig(enabled=True, block_on_partial=False)
        config = QualityGatesConfig(enabled=True, lint=False, type_check=False, tests=False, intent_verification=iv_cfg)
        task = self._make_task_with_summary()
        mock_verdict = IntentVerdict(verdict="partially", reason="Missing one part.", model="test-model")
        with patch("bernstein.core.quality_gates._run_intent_gate", return_value=(mock_verdict, False)):
            result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        iv_result = next(r for r in result.gate_results if r.gate == "intent_verification")
        assert iv_result.passed
        assert not iv_result.blocked

    def test_enabled_partial_verdict_blocks_when_configured(self, tmp_path: Path) -> None:
        """verdict=partially with block_on_partial=True → blocks."""
        iv_cfg = IntentVerificationConfig(enabled=True, block_on_partial=True)
        config = QualityGatesConfig(enabled=True, lint=False, type_check=False, tests=False, intent_verification=iv_cfg)
        task = self._make_task_with_summary()
        mock_verdict = IntentVerdict(verdict="partially", reason="Missing one part.", model="test-model")
        with patch("bernstein.core.quality_gates._run_intent_gate", return_value=(mock_verdict, True)):
            result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert not result.passed

    def test_records_intent_metric(self, tmp_path: Path) -> None:
        """Intent verification result is written to the metrics file."""
        iv_cfg = IntentVerificationConfig(enabled=True)
        config = QualityGatesConfig(enabled=True, lint=False, type_check=False, tests=False, intent_verification=iv_cfg)
        task = self._make_task_with_summary()
        mock_verdict = IntentVerdict(verdict="yes", reason="Good.", model="test-model")
        with patch("bernstein.core.quality_gates._run_intent_gate", return_value=(mock_verdict, False)):
            run_quality_gates(task, tmp_path, tmp_path, config)
        metrics_file = tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl"
        assert metrics_file.exists()
        events = [json.loads(line) for line in metrics_file.read_text().splitlines() if line.strip()]
        intent_events = [e for e in events if e["gate"] == "intent_verification"]
        assert len(intent_events) == 1
        assert intent_events[0]["verdict"] == "yes"
        assert intent_events[0]["model"] == "test-model"

    def test_intent_gate_detail_contains_verdict_and_reason(self, tmp_path: Path) -> None:
        """Gate detail string includes verdict and reason for operator visibility."""
        iv_cfg = IntentVerificationConfig(enabled=True)
        config = QualityGatesConfig(enabled=True, lint=False, type_check=False, tests=False, intent_verification=iv_cfg)
        task = self._make_task_with_summary()
        mock_verdict = IntentVerdict(verdict="no", reason="Completely wrong.", model="m")
        with patch("bernstein.core.quality_gates._run_intent_gate", return_value=(mock_verdict, True)):
            result = run_quality_gates(task, tmp_path, tmp_path, config)
        iv_result = next(r for r in result.gate_results if r.gate == "intent_verification")
        assert "no" in iv_result.detail
        assert "Completely wrong." in iv_result.detail


# ---------------------------------------------------------------------------
# IntentVerificationConfig defaults
# ---------------------------------------------------------------------------


class TestIntentVerificationConfig:
    def test_disabled_by_default(self) -> None:
        cfg = IntentVerificationConfig()
        assert not cfg.enabled

    def test_block_on_no_default_true(self) -> None:
        cfg = IntentVerificationConfig()
        assert cfg.block_on_no

    def test_block_on_partial_default_false(self) -> None:
        cfg = IntentVerificationConfig()
        assert not cfg.block_on_partial

    def test_default_model_is_cheap(self) -> None:
        cfg = IntentVerificationConfig()
        assert "flash" in cfg.model or "haiku" in cfg.model
