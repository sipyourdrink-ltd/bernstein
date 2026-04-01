"""Unit tests for p1-0002: shell completions command."""

from __future__ import annotations

from click.testing import CliRunner

from bernstein.cli.main import cli


def test_completions_bash_outputs_script() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["completions", "--shell", "bash"])
    assert result.exit_code == 0
    # Click's BashComplete generates a function + completion var
    assert "_bernstein_completion" in result.output
    assert "_BERNSTEIN_COMPLETE" in result.output


def test_completions_zsh_outputs_script() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["completions", "--shell", "zsh"])
    assert result.exit_code == 0
    assert "#compdef bernstein" in result.output
    assert "_BERNSTEIN_COMPLETE" in result.output


def test_completions_fish_outputs_script() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["completions", "--shell", "fish"])
    assert result.exit_code == 0
    assert "_BERNSTEIN_COMPLETE" in result.output


def test_completions_default_is_bash() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["completions"])
    assert result.exit_code == 0
    # Default shell is bash — should contain bash-specific function
    assert "_bernstein_completion" in result.output


def test_completions_invalid_shell_fails() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["completions", "--shell", "powershell"])
    assert result.exit_code != 0
