"""Tests for automated quality gates: lint, type-check, and test gates."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.quality_gates import (
    QualityGatesConfig,
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


# ---------------------------------------------------------------------------
# run_quality_gates: lint gate
# ---------------------------------------------------------------------------


class TestLintGate:
    def test_lint_pass_no_violations(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(enabled=True, lint=True, lint_command="exit 0", type_check=False, tests=False)
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        assert len(result.gate_results) == 1
        assert result.gate_results[0].gate == "lint"
        assert result.gate_results[0].passed
        assert not result.gate_results[0].blocked

    def test_lint_fail_blocks_merge(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(enabled=True, lint=True, lint_command="exit 1", type_check=False, tests=False)
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert not result.passed
        assert result.gate_results[0].gate == "lint"
        assert not result.gate_results[0].passed
        assert result.gate_results[0].blocked  # hard block

    def test_lint_skipped_when_disabled(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(enabled=True, lint=False, type_check=False, tests=False)
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
        config = QualityGatesConfig(enabled=True, lint=False, type_check=True, type_check_command="exit 0", tests=False)
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        assert result.gate_results[0].gate == "type_check"

    def test_type_check_fail_blocks(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(enabled=True, lint=False, type_check=True, type_check_command="exit 1", tests=False)
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert not result.passed
        assert result.gate_results[0].blocked


# ---------------------------------------------------------------------------
# run_quality_gates: test gate
# ---------------------------------------------------------------------------


class TestTestGate:
    def test_tests_pass(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(enabled=True, lint=False, type_check=False, tests=True, test_command="exit 0")
        task = _make_task()
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        assert result.gate_results[0].gate == "tests"

    def test_tests_fail_blocks(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(enabled=True, lint=False, type_check=False, tests=True, test_command="exit 1")
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
        config = QualityGatesConfig(enabled=True, lint=True, lint_command="exit 0", type_check=False, tests=False)
        task = _make_task(id="T-metrics-1")
        run_quality_gates(task, tmp_path, tmp_path, config)

        metrics_file = tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl"
        assert metrics_file.exists()
        line = json.loads(metrics_file.read_text().strip())
        assert line["task_id"] == "T-metrics-1"
        assert line["gate"] == "lint"
        assert line["result"] == "pass"

    def test_records_blocked_event(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(enabled=True, lint=True, lint_command="exit 1", type_check=False, tests=False)
        task = _make_task(id="T-metrics-2")
        run_quality_gates(task, tmp_path, tmp_path, config)

        metrics_file = tmp_path / ".sdd" / "metrics" / "quality_gates.jsonl"
        line = json.loads(metrics_file.read_text().strip())
        assert line["result"] == "blocked"

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
