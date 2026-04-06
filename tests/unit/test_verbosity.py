"""Tests for CLI-005: --verbose and --quiet flags."""

from __future__ import annotations

from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.cli.verbosity import (
    NORMAL,
    QUIET,
    VERBOSE,
    get_verbosity,
    is_quiet,
    is_verbose,
)


class TestVerbosityFlags:
    """Tests for --verbose and --quiet global flags."""

    def test_verbose_flag_accepted(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--verbose", "doctor", "--help"])
        assert result.exit_code == 0

    def test_quiet_flag_accepted(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--quiet", "doctor", "--help"])
        assert result.exit_code == 0

    def test_short_verbose_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["-v", "doctor", "--help"])
        assert result.exit_code == 0

    def test_short_quiet_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["-q", "doctor", "--help"])
        assert result.exit_code == 0

    def test_verbose_and_quiet_conflict(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--verbose", "--quiet", "doctor", "--help"])
        assert result.exit_code != 0
        assert "cannot" in result.output.lower() or "error" in result.output.lower()

    def test_verbosity_constants(self) -> None:
        assert QUIET == -1
        assert NORMAL == 0
        assert VERBOSE == 1

    def test_get_verbosity_default(self) -> None:
        """get_verbosity returns NORMAL when no Click context."""
        assert get_verbosity() == NORMAL

    def test_is_verbose_default(self) -> None:
        assert is_verbose() is False

    def test_is_quiet_default(self) -> None:
        assert is_quiet() is False
