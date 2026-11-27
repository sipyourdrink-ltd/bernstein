"""Integration tests for the ProgramBench CLI wiring (TREND-1404).

Exercises the ``bernstein benchmark programbench`` subcommand end to end
with synthetic fixtures: dataset loader, partial-credit scoring, JSON
output schema, and per-task persistence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from bernstein.cli.eval_benchmark_cmd import benchmark_group
from click.testing import CliRunner

from bernstein.benchmark.programbench import ProgramBenchHarness, ProgramBenchTask


def _dataset(tmp_path: Path, tasks: list[dict[str, Any]]) -> Path:
    path = tmp_path / "programbench.jsonl"
    path.write_text(
        "\n".join(json.dumps(t) for t in tasks) + "\n",
        encoding="utf-8",
    )
    return path


def _run_in_tmp(tmp_path: Path, args: list[str]) -> Any:
    runner = CliRunner()
    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        return runner.invoke(benchmark_group, args)
    finally:
        os.chdir(old_cwd)


@pytest.fixture(autouse=True)
def isolated_benchmark_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run fixed synthetic fixtures at the isolated-executor boundary."""
    import subprocess
    import sys

    from bernstein.benchmark import programbench

    def run_fixture(_image, argv, *, input_data, timeout):
        return subprocess.run(
            [sys.executable, *argv[1:]], input=input_data, capture_output=True, timeout=timeout, check=False
        )

    monkeypatch.setattr(programbench, "run_isolated", run_fixture)


class TestProgramBenchCLI:
    def test_command_registered(self) -> None:
        assert "programbench" in benchmark_group.commands

    def test_runtime_failure_stops_before_adapter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.benchmark import programbench
        from bernstein.core.sandbox.isolated_process import IsolationUnavailable

        def unavailable(*args, **kwargs):
            raise IsolationUnavailable("test runtime unavailable")

        monkeypatch.setattr(programbench, "run_isolated", unavailable)
        dataset = _dataset(tmp_path, [{"task_id": "pb-001", "asserts": ["True"]}])
        with patch.object(ProgramBenchHarness, "_invoke_adapter") as invoke:
            result = _run_in_tmp(tmp_path, ["programbench", "--adapter", "mock", "--dataset", str(dataset)])
        assert result.exit_code != 0
        assert "test runtime unavailable" in result.output
        invoke.assert_not_called()

    def test_runs_against_synthetic_dataset(self, tmp_path: Path) -> None:
        dataset = _dataset(
            tmp_path,
            [
                {
                    "task_id": "pb-001",
                    "asserts": ["x == 1"],
                    "setup_code": "x = 1",
                },
                {
                    "task_id": "pb-002",
                    "asserts": ["x == 2"],
                    "setup_code": "x = 1",
                },
            ],
        )

        def fake_invoke(self: ProgramBenchHarness, adapter: str, t: ProgramBenchTask) -> tuple[str, float, float]:
            return "", 0.01, 0.5

        with patch.object(ProgramBenchHarness, "_invoke_adapter", new=fake_invoke):
            result = _run_in_tmp(
                tmp_path,
                ["programbench", "--adapter", "mock", "--dataset", str(dataset)],
            )

        assert result.exit_code == 0, result.output
        assert "ProgramBench evaluation" in result.output
        assert "pb-001" in result.output
        assert "pb-002" in result.output

        metrics = tmp_path / ".sdd" / "metrics" / "programbench_results.jsonl"
        assert metrics.exists()
        record = json.loads(metrics.read_text(encoding="utf-8").splitlines()[0])
        assert record["total_tasks"] == 2
        assert record["fully_solved"] == 1
        assert record["failed"] == 1

    def test_partial_credit_propagates_to_report(self, tmp_path: Path) -> None:
        dataset = _dataset(
            tmp_path,
            [
                {
                    "task_id": "pb-partial",
                    "asserts": ["x == 1", "y == 2"],
                    "setup_code": "x = 1\ny = 99",
                }
            ],
        )

        def fake_invoke(self: ProgramBenchHarness, adapter: str, t: ProgramBenchTask) -> tuple[str, float, float]:
            return "", 0.05, 0.2

        with patch.object(ProgramBenchHarness, "_invoke_adapter", new=fake_invoke):
            result = _run_in_tmp(
                tmp_path,
                ["programbench", "--adapter", "mock", "--dataset", str(dataset)],
            )

        assert result.exit_code == 0, result.output
        metrics = tmp_path / ".sdd" / "metrics" / "programbench_results.jsonl"
        record = json.loads(metrics.read_text(encoding="utf-8").splitlines()[0])
        per_task = record["per_task"][0]
        assert per_task["score"] == 0.5
        assert per_task["asserts_passed"] == 1
        assert per_task["asserts_total"] == 2
        assert per_task["fully_solved"] is False

    def test_json_output_schema(self, tmp_path: Path) -> None:
        dataset = _dataset(
            tmp_path,
            [
                {
                    "task_id": "pb-json",
                    "asserts": ["1 == 1"],
                    "setup_code": "",
                }
            ],
        )
        out = tmp_path / "report.json"

        def fake_invoke(self: ProgramBenchHarness, adapter: str, t: ProgramBenchTask) -> tuple[str, float, float]:
            return "", 0.0, 0.01

        with patch.object(ProgramBenchHarness, "_invoke_adapter", new=fake_invoke):
            result = _run_in_tmp(
                tmp_path,
                [
                    "programbench",
                    "--adapter",
                    "mock",
                    "--dataset",
                    str(dataset),
                    "--out",
                    str(out),
                    "--no-save",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(out.read_text(encoding="utf-8"))
        for key in (
            "total_tasks",
            "fully_solved",
            "near_solved",
            "failed",
            "mean_partial_credit",
            "total_cost_usd",
            "per_task",
            "per_adapter_breakdown",
        ):
            assert key in payload, f"missing key {key}"
        assert payload["total_tasks"] == 1
        assert payload["per_task"][0]["task_id"] == "pb-json"

    def test_empty_dataset_returns_nonzero(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        result = _run_in_tmp(
            tmp_path,
            ["programbench", "--adapter", "mock", "--dataset", str(empty)],
        )
        assert result.exit_code == 1
        assert "No ProgramBench tasks found" in result.output

    def test_sandbox_failure_mode_maps_to_zero_score(self, tmp_path: Path) -> None:
        dataset = _dataset(
            tmp_path,
            [
                {
                    "task_id": "pb-sandbox-fail",
                    "asserts": ["x == 1"],
                    "setup_code": "raise RuntimeError('boom')",
                }
            ],
        )

        def fake_invoke(self: ProgramBenchHarness, adapter: str, t: ProgramBenchTask) -> tuple[str, float, float]:
            return "", 0.0, 0.0

        with patch.object(ProgramBenchHarness, "_invoke_adapter", new=fake_invoke):
            result = _run_in_tmp(
                tmp_path,
                ["programbench", "--adapter", "mock", "--dataset", str(dataset), "--no-save"],
            )

        assert result.exit_code == 0, result.output
        # The setup raises and asserts cannot run; score must be 0.
        assert "0%" in result.output or "0.00" in result.output

    def test_adapter_required(self, tmp_path: Path) -> None:
        result = _run_in_tmp(tmp_path, ["programbench"])
        # Click reports missing required option with non-zero exit.
        assert result.exit_code != 0
        assert "adapter" in result.output.lower()
