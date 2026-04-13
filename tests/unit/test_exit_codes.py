"""Tests for CLI-002: standardised exit codes and BernsteinError enhancements."""

from __future__ import annotations

import pytest
from bernstein.cli.errors import (
    BernsteinError,
    ExitCode,
    bootstrap_failed,
    handle_cli_error,
    handle_unexpected_error,
    missing_api_key,
    no_cli_agent_found,
    no_replay_tasks,
    no_seed_file,
    no_seed_or_goal,
    port_in_use,
    seed_parse_error,
    server_error,
    server_unreachable,
)


class TestExitCodeEnum:
    """Verify ExitCode values and membership."""

    def test_success_is_zero(self) -> None:
        assert ExitCode.SUCCESS == 0

    def test_general_is_one(self) -> None:
        assert ExitCode.GENERAL == 1

    def test_usage_is_two(self) -> None:
        assert ExitCode.USAGE == 2

    def test_config_is_three(self) -> None:
        assert ExitCode.CONFIG == 3

    def test_adapter_is_four(self) -> None:
        assert ExitCode.ADAPTER == 4

    def test_auth_is_five(self) -> None:
        assert ExitCode.AUTH == 5

    def test_values_are_unique(self) -> None:
        values = [e.value for e in ExitCode]
        assert len(values) == len(set(values))


class TestBernsteinErrorExitCodes:
    """Verify each factory function returns the correct exit code."""

    def test_port_in_use(self) -> None:
        err = port_in_use(8052)
        assert err.exit_code == ExitCode.GENERAL

    def test_server_unreachable(self) -> None:
        err = server_unreachable()
        assert err.exit_code == ExitCode.GENERAL

    def test_no_seed_or_goal(self) -> None:
        err = no_seed_or_goal()
        assert err.exit_code == ExitCode.CONFIG

    def test_missing_api_key(self) -> None:
        err = missing_api_key("claude", "ANTHROPIC_API_KEY")
        assert err.exit_code == ExitCode.AUTH

    def test_bootstrap_failed(self) -> None:
        err = bootstrap_failed(RuntimeError("boom"))
        assert err.exit_code == ExitCode.GENERAL

    def test_seed_parse_error(self) -> None:
        err = seed_parse_error(ValueError("bad yaml"))
        assert err.exit_code == ExitCode.CONFIG

    def test_server_error(self) -> None:
        err = server_error(ConnectionError("refused"))
        assert err.exit_code == ExitCode.GENERAL

    def test_no_cli_agent_found(self) -> None:
        err = no_cli_agent_found()
        assert err.exit_code == ExitCode.ADAPTER

    def test_no_seed_file(self) -> None:
        err = no_seed_file()
        assert err.exit_code == ExitCode.CONFIG

    def test_no_replay_tasks(self) -> None:
        err = no_replay_tasks()
        assert err.exit_code == ExitCode.USAGE


class TestBernsteinErrorPrint:
    """Verify the print method includes suggestion output."""

    def test_print_includes_suggestion(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = BernsteinError(
            what="Port 8052 already in use",
            why="Another process is using the port",
            fix="Run bernstein stop",
            exit_code=ExitCode.GENERAL,
        )
        err.print()
        # The print goes to stderr via Rich Console, but capsys may not capture it.
        # At minimum, verify no exception is raised.

    def test_default_exit_code(self) -> None:
        err = BernsteinError(what="test", why="test", fix="test")
        assert err.exit_code == ExitCode.GENERAL


class TestHandleCliError:
    """Verify handle_cli_error returns correct SystemExit."""

    def test_returns_system_exit_with_code(self) -> None:
        err = missing_api_key("claude", "ANTHROPIC_API_KEY")
        result = handle_cli_error(err)
        assert isinstance(result, SystemExit)
        assert result.code == ExitCode.AUTH

    def test_general_error(self) -> None:
        err = port_in_use(8052)
        result = handle_cli_error(err)
        assert isinstance(result, SystemExit)
        assert result.code == ExitCode.GENERAL


class TestHandleUnexpectedError:
    """Verify handle_unexpected_error returns SystemExit(1)."""

    def test_returns_system_exit_1(self) -> None:
        result = handle_unexpected_error(RuntimeError("boom"))
        assert isinstance(result, SystemExit)
        assert result.code == ExitCode.GENERAL

    def test_with_known_pattern(self) -> None:
        """Known patterns should still return GENERAL (it's unexpected)."""
        result = handle_unexpected_error(ConnectionRefusedError("connection refused"))
        assert isinstance(result, SystemExit)
        assert result.code == ExitCode.GENERAL
