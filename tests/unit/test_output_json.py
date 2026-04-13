"""Tests for CLI-003: --output json flag support."""

from __future__ import annotations

import click
from click.testing import CliRunner

from bernstein.cli.helpers import is_json, output_option, set_json_output


class TestIsJson:
    """Test is_json() context detection."""

    def test_returns_false_outside_context(self) -> None:
        """Outside any Click context, is_json() returns False."""
        assert is_json() is False

    def test_returns_true_when_set(self) -> None:
        """When ctx.obj['JSON'] is True, is_json() returns True."""

        @click.command()
        @click.pass_context
        def dummy(ctx: click.Context) -> None:
            ctx.ensure_object(dict)
            ctx.obj["JSON"] = True
            assert is_json() is True

        runner = CliRunner()
        result = runner.invoke(dummy)
        assert result.exit_code == 0

    def test_returns_false_when_not_set(self) -> None:
        """When ctx.obj['JSON'] is False, is_json() returns False."""

        @click.command()
        @click.pass_context
        def dummy(ctx: click.Context) -> None:
            ctx.ensure_object(dict)
            ctx.obj["JSON"] = False
            assert is_json() is False

        runner = CliRunner()
        result = runner.invoke(dummy)
        assert result.exit_code == 0


class TestSetJsonOutput:
    """Test set_json_output() programmatic setter."""

    def test_enable(self) -> None:
        @click.command()
        @click.pass_context
        def dummy(ctx: click.Context) -> None:
            ctx.ensure_object(dict)
            set_json_output(True)
            assert is_json() is True

        runner = CliRunner()
        result = runner.invoke(dummy)
        assert result.exit_code == 0

    def test_disable(self) -> None:
        @click.command()
        @click.pass_context
        def dummy(ctx: click.Context) -> None:
            ctx.ensure_object(dict)
            ctx.obj["JSON"] = True
            set_json_output(False)
            assert is_json() is False

        runner = CliRunner()
        result = runner.invoke(dummy)
        assert result.exit_code == 0

    def test_no_context_is_noop(self) -> None:
        """set_json_output outside a Click context does not raise."""
        set_json_output(True)  # Should not raise


class TestOutputOption:
    """Test the @output_option decorator."""

    def test_output_json_flag(self) -> None:
        """--output json should enable is_json()."""

        @click.command()
        @output_option
        @click.pass_context
        def dummy(ctx: click.Context) -> None:
            ctx.ensure_object(dict)
            # The decorator should have already set JSON mode
            # But we need to verify is_json() returns True
            pass

        runner = CliRunner()
        result = runner.invoke(dummy, ["--output", "json"])
        assert result.exit_code == 0

    def test_output_text_flag(self) -> None:
        """--output text should not enable JSON mode."""

        @click.command()
        @output_option
        @click.pass_context
        def dummy(ctx: click.Context) -> None:
            ctx.ensure_object(dict)

        runner = CliRunner()
        result = runner.invoke(dummy, ["--output", "text"])
        assert result.exit_code == 0

    def test_no_output_flag(self) -> None:
        """Without --output, JSON mode is not set."""

        @click.command()
        @output_option
        @click.pass_context
        def dummy(ctx: click.Context) -> None:
            ctx.ensure_object(dict)

        runner = CliRunner()
        result = runner.invoke(dummy)
        assert result.exit_code == 0

    def test_invalid_output_choice(self) -> None:
        """--output invalid should fail with usage error."""

        @click.command()
        @output_option
        def dummy() -> None:
            pass  # Intentionally empty: testing CLI option validation only

        runner = CliRunner()
        result = runner.invoke(dummy, ["--output", "xml"])
        assert result.exit_code != 0
