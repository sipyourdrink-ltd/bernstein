"""Tests for CLI-008: ``bernstein explain <command>``."""

from __future__ import annotations

from bernstein.cli.explain_help_cmd import explain_help_cmd
from click.testing import CliRunner

from bernstein.cli.main import cli


class TestExplainHelpCmd:
    """Tests for the explain command."""

    def test_explain_command_exists(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["explain", "--help"])
        assert result.exit_code == 0

    def test_explain_lists_commands(self) -> None:
        """Running explain without args lists available commands."""
        runner = CliRunner()
        result = runner.invoke(explain_help_cmd, [])
        assert result.exit_code == 0
        assert "run" in result.output
        assert "doctor" in result.output

    def test_explain_run_shows_examples(self) -> None:
        runner = CliRunner()
        result = runner.invoke(explain_help_cmd, ["run"])
        assert result.exit_code == 0
        assert "bernstein run" in result.output
        assert "Examples" in result.output

    def test_explain_doctor_shows_examples(self) -> None:
        runner = CliRunner()
        result = runner.invoke(explain_help_cmd, ["doctor"])
        assert result.exit_code == 0
        assert "bernstein doctor" in result.output

    def test_explain_unknown_command(self) -> None:
        runner = CliRunner()
        result = runner.invoke(explain_help_cmd, ["nonexistent_cmd"])
        assert result.exit_code == 0
        assert "no detailed help" in result.output.lower()

    def test_explain_replay_shows_filter_examples(self) -> None:
        runner = CliRunner()
        result = runner.invoke(explain_help_cmd, ["replay"])
        assert result.exit_code == 0
        assert "bernstein replay" in result.output

    def test_explain_diff_shows_examples(self) -> None:
        runner = CliRunner()
        result = runner.invoke(explain_help_cmd, ["diff"])
        assert result.exit_code == 0
        assert "bernstein diff" in result.output

    def test_explain_shows_related_commands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(explain_help_cmd, ["run"])
        assert result.exit_code == 0
        assert "Related" in result.output

    def test_explain_shows_tips(self) -> None:
        runner = CliRunner()
        result = runner.invoke(explain_help_cmd, ["run"])
        assert result.exit_code == 0
        assert "Tips" in result.output
