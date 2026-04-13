"""Tests for CLI-013: command aliases and shortcuts."""

from __future__ import annotations

from bernstein.cli.aliases import ALIASES, aliases_cmd, get_alias, get_all_aliases
from click.testing import CliRunner

from bernstein.cli.main import cli


class TestAliases:
    """Tests for the aliases module."""

    def test_aliases_defined(self) -> None:
        """Core aliases should be defined."""
        assert "s" in ALIASES
        assert "r" in ALIASES
        assert "d" in ALIASES

    def test_alias_s_maps_to_score(self) -> None:
        assert ALIASES["s"] == "score"

    def test_alias_r_maps_to_run(self) -> None:
        assert ALIASES["r"] == "run"

    def test_alias_d_maps_to_doctor(self) -> None:
        assert ALIASES["d"] == "doctor"

    def test_alias_l_maps_to_live(self) -> None:
        assert ALIASES["l"] == "live"

    def test_get_alias_found(self) -> None:
        assert get_alias("s") == "score"

    def test_get_alias_not_found(self) -> None:
        assert get_alias("zzz") is None

    def test_get_all_aliases(self) -> None:
        all_aliases = get_all_aliases()
        assert isinstance(all_aliases, dict)
        assert len(all_aliases) > 0
        # Modifying the copy should not affect the original
        all_aliases["test_key"] = "test_val"
        assert "test_key" not in ALIASES


class TestAliasesCmd:
    """Tests for the aliases command."""

    def test_aliases_command_exists(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["aliases", "--help"])
        assert result.exit_code == 0

    def test_aliases_shows_table(self) -> None:
        runner = CliRunner()
        result = runner.invoke(aliases_cmd, [])
        assert result.exit_code == 0
        assert "score" in result.output
        assert "run" in result.output
        assert "doctor" in result.output


class TestAliasResolution:
    """Tests for alias resolution in the main CLI."""

    def test_s_resolves_to_score_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["s", "--help"])
        # Should resolve to score (score) command help
        assert result.exit_code == 0

    def test_d_resolves_to_doctor_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["d", "--help"])
        assert result.exit_code == 0
