"""Tests for CLI-004: shell completions for bash, zsh, fish."""

from __future__ import annotations

from click.testing import CliRunner

from bernstein.cli.main import cli


class TestCompletionsCommand:
    """Tests for the completions command."""

    def test_completions_bash_outputs_script(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["completions", "--shell", "bash"])
        assert result.exit_code == 0
        assert "_bernstein_completion" in result.output
        assert "_BERNSTEIN_COMPLETE" in result.output

    def test_completions_zsh_outputs_script(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["completions", "--shell", "zsh"])
        assert result.exit_code == 0
        assert "#compdef bernstein" in result.output
        assert "_BERNSTEIN_COMPLETE" in result.output

    def test_completions_fish_outputs_script(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["completions", "--shell", "fish"])
        assert result.exit_code == 0
        assert "_BERNSTEIN_COMPLETE" in result.output

    def test_completions_default_is_bash(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["completions"])
        assert result.exit_code == 0
        assert "_bernstein_completion" in result.output

    def test_completions_invalid_shell_fails(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["completions", "--shell", "powershell"])
        assert result.exit_code != 0

    def test_completions_help_text(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["completions", "--help"])
        assert result.exit_code == 0
        assert "bash" in result.output.lower()
        assert "zsh" in result.output.lower()
        assert "fish" in result.output.lower()
