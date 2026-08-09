"""Unit tests for #3140: Folding quickstart, init-wizard, validate, and routine into domain owners."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.cli.utils.aliases import ALIASES


def test_demo_flask_todo_option_exists() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["demo", "--help"])
    assert result.exit_code == 0
    assert "--flask-todo" in result.output


def test_init_wizard_option_exists() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--help"])
    assert result.exit_code == 0
    assert "--wizard" in result.output


def test_alias_i_repointed_to_init() -> None:
    assert ALIASES.get("i") == "init"


def test_plan_validate_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["plan", "validate", "--help"])
    assert result.exit_code == 0
    assert "Validate a plan file" in result.output


def test_schedule_routine_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["schedule", "routine", "--help"])
    assert result.exit_code == 0


def test_deprecated_top_level_aliases_warning(tmp_path: Path) -> None:
    runner = CliRunner()
    res_qs = runner.invoke(cli, ["quickstart", "--timeout", "1"])
    assert (
        "WARNING: 'bernstein quickstart' is deprecated" in res_qs.output
        or "WARNING: 'bernstein quickstart' is deprecated" in res_qs.stderr
    )

    res_wiz = runner.invoke(cli, ["init-wizard", "--non-interactive", "--dir", str(tmp_path)])
    assert (
        "WARNING: 'bernstein init-wizard' is deprecated" in res_wiz.output
        or "WARNING: 'bernstein init-wizard' is deprecated" in res_wiz.stderr
    )

    dummy_plan = tmp_path / "plan.yaml"
    dummy_plan.write_text("stages:\n  - name: test\n    steps: []\n")
    res_val = runner.invoke(cli, ["validate", str(dummy_plan)])
    assert (
        "WARNING: 'bernstein validate' is deprecated" in res_val.output
        or "WARNING: 'bernstein validate' is deprecated" in res_val.stderr
    )

    res_rout = runner.invoke(cli, ["routine", "scenarios"])
    assert (
        "WARNING: 'bernstein routine' is deprecated" in res_rout.output
        or "WARNING: 'bernstein routine' is deprecated" in res_rout.stderr
    )
