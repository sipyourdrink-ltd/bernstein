"""Tests for the ``bernstein insights`` CLI command (#5880).

Regression coverage for two defects: the command was implemented but never
registered with the top-level CLI group, so ``bernstein insights`` raised
"No such command"; and its ``--format json`` branch called
``console.print_json`` with a raw ``dict`` positionally, which raises
``TypeError`` because that parameter expects a JSON string (or the object
passed via the ``data=`` keyword).
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from bernstein.cli.main import cli


def test_insights_is_registered_on_the_top_level_cli() -> None:
    """``bernstein insights`` is a real subcommand, not "No such command"."""
    runner = CliRunner()
    result = runner.invoke(cli, ["insights", "--help"])

    assert result.exit_code == 0, result.output
    assert "No such command" not in result.output
    assert "Show analytics and insights from task traces" in result.output


def test_insights_text_format_runs_without_error() -> None:
    """Default (text) output succeeds and echoes the requested window."""
    runner = CliRunner()
    result = runner.invoke(cli, ["insights", "--since", "1d"])

    assert result.exit_code == 0, result.output
    assert "1d" in result.output


def test_insights_json_format_emits_parseable_json() -> None:
    """``--format json`` must not crash, and must emit valid JSON."""
    runner = CliRunner()
    result = runner.invoke(cli, ["insights", "--format", "json", "--since", "30d"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["since"] == "30d"
