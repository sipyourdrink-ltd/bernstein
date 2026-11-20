"""Unit tests for the ``bernstein simulate`` CLI surface (issue #1374).

Covers:

* Happy path: plan file -> markdown summary on stdout.
* JSON output mode.
* ``--out`` sidecar (JSON + Markdown).
* ``--budget-cap`` exit code on breach.
* Missing plan -> non-zero exit.
* ``--seed`` propagates into the report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from bernstein.cli.commands.simulate_cmd import simulate_cmd
from bernstein.cli.main import cli

_PLAN: dict[str, object] = {
    "name": "CLI demo plan",
    "stages": [
        {
            "name": "Build",
            "steps": [
                {"title": "Add endpoint", "role": "backend"},
                {"title": "Test endpoint", "role": "qa"},
            ],
        }
    ],
}


@pytest.fixture
def plan_path(tmp_path: Path) -> Path:
    target = tmp_path / "plan.yaml"
    target.write_text(yaml.safe_dump(_PLAN), encoding="utf-8")
    return target


def test_simulate_cli_happy_path_markdown(plan_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(simulate_cmd, [str(plan_path)])
    assert result.exit_code == 0, result.output
    assert "# Bernstein simulate" in result.output


def test_simulate_cli_json_format(plan_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(simulate_cmd, [str(plan_path), "--format", "json"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["plan_name"] == "CLI demo plan"


def test_simulate_cli_out_json_sidecar(plan_path: Path, tmp_path: Path) -> None:
    out_path = tmp_path / "report.json"
    runner = CliRunner()
    result = runner.invoke(simulate_cmd, [str(plan_path), "--out", str(out_path)])
    assert result.exit_code == 0, result.output
    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert parsed["plan_name"] == "CLI demo plan"


def test_simulate_cli_out_markdown_sidecar(plan_path: Path, tmp_path: Path) -> None:
    out_path = tmp_path / "report.md"
    runner = CliRunner()
    result = runner.invoke(simulate_cmd, [str(plan_path), "--out", str(out_path)])
    assert result.exit_code == 0, result.output
    body = out_path.read_text(encoding="utf-8")
    assert "# Bernstein simulate" in body


def test_simulate_cli_out_does_not_change_stdout_format(plan_path: Path, tmp_path: Path) -> None:
    out_path = tmp_path / "report.md"
    result = CliRunner().invoke(cli, ["simulate", str(plan_path), "--format", "json", "--out", str(out_path)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["plan_name"] == "CLI demo plan"
    assert "# Bernstein simulate" in out_path.read_text(encoding="utf-8")
    assert "Report written to" in result.stderr


def test_simulate_cli_out_unknown_extension_defaults_to_json(plan_path: Path, tmp_path: Path) -> None:
    out_path = tmp_path / "report.txt"
    runner = CliRunner()
    result = runner.invoke(simulate_cmd, [str(plan_path), "--out", str(out_path)])
    assert result.exit_code == 0, result.output
    # Unknown extension means JSON sidecar.
    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert "task_count" in parsed


def test_simulate_cli_budget_cap_breach_exits_three(plan_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(simulate_cmd, [str(plan_path), "--budget-cap", "0.0"])
    assert result.exit_code == 3
    assert "budget cap" in result.output.lower() or "budget cap" in (result.stderr or "").lower()


def test_simulate_cli_budget_cap_high_no_breach(plan_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(simulate_cmd, [str(plan_path), "--budget-cap", "10000"])
    assert result.exit_code == 0


def test_simulate_cli_missing_plan_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(simulate_cmd, [str(tmp_path / "nope.yaml")])
    # Click rejects nonexistent file in argument parsing.
    assert result.exit_code != 0


def test_simulate_cli_seed_propagates(plan_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(simulate_cmd, [str(plan_path), "--format", "json", "--seed", "7"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["seed"] == 7


def test_simulate_cli_help_lists_options() -> None:
    runner = CliRunner()
    result = runner.invoke(simulate_cmd, ["--help"])
    assert result.exit_code == 0
    assert "--from-traces" in result.output
    assert "--seed" in result.output
    assert "--budget-cap" in result.output
    assert "does not change stdout" in " ".join(result.output.split())


def test_simulate_cli_traces_dir_increments_history(plan_path: Path, tmp_path: Path) -> None:
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    (traces_dir / "t.jsonl").write_text(
        '{"role": "backend", "adapter": "mock", "status": "completed", "latency_seconds": 30}\n'
        '{"role": "qa", "adapter": "mock", "status": "completed", "latency_seconds": 5}\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        simulate_cmd,
        [str(plan_path), "--format", "json", "--traces-dir", str(traces_dir)],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["history_samples"] == 2


@pytest.mark.parametrize(("limit", "expected_p50"), [(1, 30.0), (2, 20.0)])
def test_simulate_cli_from_traces_limits_latency_samples(
    plan_path: Path, tmp_path: Path, limit: int, expected_p50: float
) -> None:
    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "local.jsonl").write_text(
        '{"role": "backend", "adapter": "mock", "status": "completed", "latency_seconds": 10}\n'
        '{"role": "backend", "adapter": "mock", "status": "completed", "latency_seconds": 30}\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli,
        [
            "simulate",
            str(plan_path),
            "--format",
            "json",
            "--seed",
            "42",
            "--traces-dir",
            str(traces),
            "--from-traces",
            str(limit),
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["history_samples"] == 2
    backend = next(task for task in report["tasks"] if task["role"] == "backend")
    assert backend["latency_p50"] == pytest.approx(expected_p50, abs=2)


def test_simulate_cli_metrics_dir_argument(plan_path: Path, tmp_path: Path) -> None:
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    (metrics_dir / "cost.jsonl").write_text(
        '{"role": "backend", "adapter": "mock", "cost_usd": 0.1}\n'
        '{"role": "backend", "adapter": "mock", "cost_usd": 0.2}\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        simulate_cmd,
        [str(plan_path), "--format", "json", "--metrics-dir", str(metrics_dir)],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["task_count"] == 2
