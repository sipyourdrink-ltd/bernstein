"""Tests for bernstein.cli.errors — structured error reporting."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from bernstein.cli.errors import (
    BernsteinError,
    bootstrap_failed,
    missing_api_key,
    no_seed_or_goal,
    port_in_use,
    seed_parse_error,
    server_error,
    server_unreachable,
)


class TestBernsteinError:
    """Test the BernsteinError dataclass."""

    def test_str_format(self) -> None:
        err = BernsteinError(what="Failed", why="Broken", fix="Fix it")
        result = str(err)
        assert "Failed" in result
        assert "Reason: Broken" in result
        assert "Fix: Fix it" in result

    def test_print_outputs_to_stderr(self) -> None:
        err = BernsteinError(what="Test error", why="Test reason", fix="Test fix")
        buf = StringIO()
        with patch("bernstein.cli.errors.console") as mock_console:
            mock_console.print = lambda *a, **kw: buf.write(str(a[0]) + "\n")
            err.print()
        output = buf.getvalue()
        assert "Test error" in output
        assert "Test reason" in output
        assert "Test fix" in output

    def test_is_exception(self) -> None:
        err = BernsteinError(what="x", why="y", fix="z")
        assert isinstance(err, Exception)


class TestErrorFactories:
    """Test the helper factory functions."""

    def test_port_in_use(self) -> None:
        err = port_in_use(8052)
        assert "8052" in err.what
        assert "Port already in use" in err.why
        assert "bernstein stop" in err.fix

    def test_server_unreachable(self) -> None:
        err = server_unreachable()
        assert "Cannot reach" in err.what
        assert "8052" in err.why
        assert "bernstein" in err.fix

    def test_no_seed_or_goal(self) -> None:
        err = no_seed_or_goal()
        assert "goal" in err.what.lower() or "seed" in err.what.lower()
        assert "bernstein.yaml" in err.fix or "-g" in err.fix

    def test_missing_api_key(self) -> None:
        err = missing_api_key("claude", "ANTHROPIC_API_KEY")
        assert "claude" in err.what
        assert "ANTHROPIC_API_KEY" in err.why
        assert "export" in err.fix

    def test_bootstrap_failed(self) -> None:
        err = bootstrap_failed(RuntimeError("Server crashed"))
        assert "Bootstrap failed" in err.what
        assert "Server crashed" in err.why
        assert "doctor" in err.fix

    def test_seed_parse_error(self) -> None:
        err = seed_parse_error(ValueError("Invalid YAML"))
        assert "parse" in err.what.lower() or "seed" in err.what.lower()
        assert "Invalid YAML" in err.why

    def test_server_error(self) -> None:
        err = server_error(Exception("Connection refused"))
        assert "server" in err.what.lower()
        assert "Connection refused" in err.why
        assert "status" in err.fix
