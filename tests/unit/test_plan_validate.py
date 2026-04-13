"""Tests for the `bernstein validate` CLI command."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from bernstein.cli.plan_validate_cmd import validate_plan
from click.testing import CliRunner


def _write_plan(tmp_path: Path, data: object) -> Path:
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text(yaml.dump(data))
    return plan_file


class TestValidatePlan:
    """Tests for plan validate command."""

    @pytest.fixture()
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_valid_plan(self, runner: CliRunner, tmp_path: Path) -> None:
        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Valid Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [
                            {"title": "Task A", "role": "backend"},
                            {"title": "Task B", "role": "qa"},
                        ],
                    },
                    {
                        "name": "Stage 2",
                        "depends_on": ["Stage 1"],
                        "steps": [
                            {"title": "Task C", "role": "frontend"},
                        ],
                    },
                ],
            },
        )
        result = runner.invoke(validate_plan, [str(plan_file)])
        assert result.exit_code == 0
        assert "Plan is valid" in result.output
        assert "Stages: 2" in result.output
        assert "Tasks: 3" in result.output

    def test_duplicate_titles(self, runner: CliRunner, tmp_path: Path) -> None:
        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Dup Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [
                            {"title": "Same Title", "role": "backend"},
                            {"title": "Same Title", "role": "qa"},
                        ],
                    },
                ],
            },
        )
        result = runner.invoke(validate_plan, [str(plan_file)])
        assert result.exit_code == 1
        assert "Duplicate task title" in result.output

    def test_unknown_role_warning(self, runner: CliRunner, tmp_path: Path) -> None:
        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Unknown Role Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [
                            {"title": "Task A", "role": "wizard"},
                        ],
                    },
                ],
            },
        )
        result = runner.invoke(validate_plan, [str(plan_file)])
        # Unknown role is a warning, not an error -- plan is still valid
        assert result.exit_code == 0
        assert "unknown role" in result.output
        assert "wizard" in result.output
        assert "warning" in result.output.lower()

    def test_invalid_yaml(self, runner: CliRunner, tmp_path: Path) -> None:
        plan_file = tmp_path / "bad.yaml"
        plan_file.write_text("not: a: valid: [plan")
        result = runner.invoke(validate_plan, [str(plan_file)])
        assert result.exit_code == 1

    def test_missing_stages(self, runner: CliRunner, tmp_path: Path) -> None:
        plan_file = _write_plan(tmp_path, {"name": "No stages"})
        result = runner.invoke(validate_plan, [str(plan_file)])
        assert result.exit_code == 1
        assert "Plan load error" in result.output

    def test_max_parallel_width(self, runner: CliRunner, tmp_path: Path) -> None:
        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Wide Plan",
                "stages": [
                    {
                        "name": "Wide Stage",
                        "steps": [{"title": f"Task {i}", "role": "backend"} for i in range(5)],
                    },
                ],
            },
        )
        result = runner.invoke(validate_plan, [str(plan_file)])
        assert result.exit_code == 0
        assert "Max parallel width: 5" in result.output


class TestDryRunWithPlanFile:
    """Tests for dry-run mode loading tasks from a plan file."""

    @pytest.fixture()
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_dry_run_with_plan_file(self, runner: CliRunner, tmp_path: Path) -> None:
        from bernstein.cli.run_cmd import run

        plan_file = _write_plan(
            tmp_path,
            {
                "name": "DryRun Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [
                            {"title": "Build API", "role": "backend", "model": "opus", "effort": "high"},
                            {"title": "Write tests", "role": "qa"},
                        ],
                    },
                ],
            },
        )
        result = runner.invoke(run, ["--dry-run", str(plan_file)])
        assert result.exit_code == 0
        assert "Dry-run mode" in result.output
        assert "Build API" in result.output
        assert "Write tests" in result.output
        assert "No agents were spawned" in result.output

    def test_dry_run_shows_cost_estimate(self, runner: CliRunner, tmp_path: Path) -> None:
        from bernstein.cli.run_cmd import run

        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Cost Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [
                            {"title": "Task 1", "role": "backend"},
                        ],
                    },
                ],
            },
        )
        result = runner.invoke(run, ["--dry-run", str(plan_file)])
        assert result.exit_code == 0
        assert "Estimated cost" in result.output
        assert "Total tasks: 1" in result.output

    def test_dry_run_shows_dependencies(self, runner: CliRunner, tmp_path: Path) -> None:
        from bernstein.cli.run_cmd import run

        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Deps Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [
                            {"title": "Setup DB", "role": "backend"},
                        ],
                    },
                    {
                        "name": "Stage 2",
                        "depends_on": ["Stage 1"],
                        "steps": [
                            {"title": "Build API", "role": "backend"},
                        ],
                    },
                ],
            },
        )
        result = runner.invoke(run, ["--dry-run", str(plan_file)])
        assert result.exit_code == 0
        assert "Dependency graph" in result.output
