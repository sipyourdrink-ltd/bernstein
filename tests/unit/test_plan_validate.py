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


class TestSchemaCheck:
    """The schema check the command's own documentation promises.

    ``load_plan`` parses a plan without judging its fields, and every other
    check reads the task graph rather than the plan document. A field the
    scheduler rejects therefore reached "Plan is valid." untouched. These pin
    each class of schema error to a nonzero exit.
    """

    @pytest.fixture()
    def runner(self) -> CliRunner:
        return CliRunner()

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            pytest.param(lambda p: p.pop("name"), "name", id="missing-name"),
            pytest.param(
                lambda p: p["stages"][0]["steps"][0].update({"priority": 0}),
                "priority",
                id="priority-below-range",
            ),
            pytest.param(
                lambda p: p["stages"][0]["steps"][0].update({"files": "src/app.py"}),
                "files",
                id="scalar-where-a-list-belongs",
            ),
            pytest.param(
                lambda p: p["stages"][0]["steps"][0].update({"complexity": "epic"}),
                "complexity",
                id="value-outside-an-enum",
            ),
            pytest.param(
                lambda p: p["stages"][0]["steps"][0].update({"estimated_minutes": "30m"}),
                "estimated_minutes",
                id="duration-written-as-prose",
            ),
            pytest.param(
                lambda p: p.update({"max_agents": "4 workers"}),
                "max_agents",
                id="count-written-as-prose",
            ),
        ],
    )
    def test_a_schema_error_is_reported_and_fails_the_command(
        self,
        runner: CliRunner,
        tmp_path: Path,
        mutate: object,
        expected: str,
    ) -> None:
        plan: dict[str, object] = {
            "name": "Schema Plan",
            "stages": [
                {
                    "name": "Stage 1",
                    "steps": [{"title": "Task A", "role": "backend"}],
                },
            ],
        }
        mutate(plan)  # type: ignore[operator]
        plan_file = _write_plan(tmp_path, plan)

        result = runner.invoke(validate_plan, [str(plan_file)])

        assert result.exit_code == 1
        assert "Plan is valid" not in result.output
        assert expected in result.output

    def test_a_schema_clean_plan_still_passes(self, runner: CliRunner, tmp_path: Path) -> None:
        """The check must not start rejecting plans the schema accepts."""
        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Schema Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [{"title": "Task A", "role": "backend", "priority": 3}],
                    },
                ],
            },
        )

        result = runner.invoke(validate_plan, [str(plan_file)])

        assert result.exit_code == 0
        assert "Plan is valid" in result.output

    def test_an_unrecognised_role_stays_a_warning(self, runner: CliRunner, tmp_path: Path) -> None:
        """The schema holds role in an enum; this command must not adopt that verdict.

        Roles come from ``templates/roles/``, which an operator extends. Taking
        the schema's role error would fail every plan naming a role of its own.
        """
        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Custom Role Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [{"title": "Task A", "role": "chaos-monkey"}],
                    },
                ],
            },
        )

        result = runner.invoke(validate_plan, [str(plan_file)])

        assert result.exit_code == 0
        assert "warning" in result.output.lower()

    @pytest.mark.parametrize(
        "role",
        ["analyst", "ci-fixer", "ml-engineer", "prompt-engineer", "resolver", "retrieval", "visionary", "vp"],
    )
    def test_a_role_that_ships_with_the_project_is_not_called_unknown(
        self,
        runner: CliRunner,
        tmp_path: Path,
        role: str,
    ) -> None:
        """Every one of these has a file under ``templates/roles/``.

        The command kept its own copy of the role list, which had drifted to a
        subset of the shipped roles, so a plan using a role the project itself
        provides was told that role was unknown.
        """
        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Shipped Role Plan",
                "stages": [{"name": "Stage 1", "steps": [{"title": "Task A", "role": role}]}],
            },
        )

        result = runner.invoke(validate_plan, [str(plan_file)])

        assert result.exit_code == 0
        assert "unknown role" not in result.output.lower()


class TestOutOfRangeEnumValues:
    """`plan validate` on an out-of-range enum reports a verdict, never a traceback.

    Issue #3515: an enum typo (`complexity: epic`) escaped `load_plan` as a bare
    ValueError, so the command crashed with a traceback instead of printing the
    validation error it exists to print. These drive the full CLI group, the
    same entry point the `bernstein` binary dispatches through.
    """

    @pytest.fixture()
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_issue_repro_exits_1_with_readable_error(self, runner: CliRunner, tmp_path: Path) -> None:
        """The issue's exact reproduction YAML, byte for byte."""
        from bernstein.cli.main import cli

        plan_file = tmp_path / "plan.yaml"
        plan_file.write_text(
            "name: Enum Plan\n"
            "stages:\n"
            "  - name: Stage 1\n"
            "    steps:\n"
            "      - title: Task A\n"
            "        role: backend\n"
            "        complexity: epic\n"
        )

        result = runner.invoke(cli, ["plan", "validate", str(plan_file)])

        assert result.exit_code == 1
        assert "complexity" in result.output
        assert "epic" in result.output
        assert "Traceback" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("complexity", "epic", id="complexity"),
            pytest.param("scope", "galactic", id="scope"),
            pytest.param("effort", "turbo", id="effort"),
        ],
    )
    def test_each_enum_field_gets_a_verdict_not_a_traceback(
        self,
        runner: CliRunner,
        tmp_path: Path,
        field: str,
        value: str,
    ) -> None:
        from bernstein.cli.main import cli

        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Enum Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [{"title": "Task A", "role": "backend", field: value}],
                    },
                ],
            },
        )

        result = runner.invoke(cli, ["plan", "validate", str(plan_file)])

        assert result.exit_code == 1
        assert field in result.output
        assert value in result.output
        assert "Traceback" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_arbitrary_model_identifier_is_valid_at_command_surface(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        from bernstein.cli.main import cli

        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Arbitrary Model Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [
                            {
                                "title": "Task A",
                                "role": "backend",
                                "model": "provider/model-name",
                            },
                        ],
                    },
                ],
            },
        )

        result = runner.invoke(cli, ["plan", "validate", str(plan_file)])

        assert result.exit_code == 0
        assert "Plan is valid." in result.output
        assert "Traceback" not in result.output
        assert result.exception is None


class TestSchemaParityAtTheCommand:
    """Issue #3516 at the command surface.

    The schema pre-check now enforces the types and ranges PLAN_JSON_SCHEMA
    declares, and reports keys the schema does not know as warnings. Errors
    must fail the command readably; warnings alone must not.
    """

    @pytest.fixture()
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_issue_repro_exits_1_with_readable_errors(self, runner: CliRunner, tmp_path: Path) -> None:
        """The issue's reproduction plan, driven through the full CLI group."""
        from bernstein.cli.main import cli

        plan_file = tmp_path / "plan.yaml"
        plan_file.write_text(
            "name: P\n"
            "max_agents: 0\n"
            "stages:\n"
            "  - name: S\n"
            "    steps:\n"
            "      - title: T\n"
            "        role: 7\n"
            "        files: [42]\n"
            "        unknown_key: 1\n"
        )

        result = runner.invoke(cli, ["plan", "validate", str(plan_file)])

        assert result.exit_code == 1
        assert "max_agents" in result.output
        assert "files[0]" in result.output
        assert "role" in result.output
        assert "Traceback" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_non_string_role_is_an_error_not_a_role_warning(self, runner: CliRunner, tmp_path: Path) -> None:
        """The role *enum* verdict stays dropped (operators extend roles); the
        role *type* verdict does not -- a non-string role is not a custom role."""
        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Typed Role Plan",
                "stages": [{"name": "Stage 1", "steps": [{"title": "Task A", "role": 7}]}],
            },
        )

        result = runner.invoke(validate_plan, [str(plan_file)])

        assert result.exit_code == 1
        assert "role" in result.output

    def test_unknown_key_alone_is_a_warning_and_exit_0(self, runner: CliRunner, tmp_path: Path) -> None:
        plan_file = _write_plan(
            tmp_path,
            {
                "name": "Extra Key Plan",
                "stages": [
                    {
                        "name": "Stage 1",
                        "steps": [{"title": "Task A", "role": "backend", "unknown_key": 1}],
                    },
                ],
            },
        )

        result = runner.invoke(validate_plan, [str(plan_file)])

        assert result.exit_code == 0
        assert "unknown_key" in result.output
        assert "warning" in result.output.lower()


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
