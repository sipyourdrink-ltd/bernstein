"""Integration tests for the first-run error categorisation CLI wiring.

We exercise the public :func:`bernstein.cli.first_run_guard.handle_first_run_exception`
through a synthetic Click command, then assert exit code + hint substring
for each category.  This avoids the cost of standing up the Bernstein
server while still verifying the full handler path end to end.
"""

from __future__ import annotations

import errno
import subprocess
from collections.abc import Callable

import click
import pytest
from click.testing import CliRunner

from bernstein.cli.first_run_guard import handle_first_run_exception
from bernstein.core.errors import (
    AdapterAuthError,
    AdapterBinaryNotFoundError,
    BernsteinFirstRunError,
    ErrorCategory,
    exit_code_for,
)


def _make_command(exc_factory: Callable[[], BaseException]) -> click.Command:
    """Build a tiny Click command whose body raises ``exc_factory()``.

    The body is wrapped by :func:`handle_first_run_exception` exactly the
    way ``bernstein run`` wraps its own body.
    """

    @click.command("bernstein-run-fake")
    @click.option("--verbose", is_flag=True, default=False)
    def cmd(verbose: bool) -> None:
        try:
            raise exc_factory()
        except (click.UsageError, SystemExit):
            raise
        except BaseException as exc:
            handle_first_run_exception(exc, verbose=verbose)

    return cmd


@pytest.fixture
def runner() -> CliRunner:
    # Click 8.3 removed ``mix_stderr`` from CliRunner; stderr is captured
    # separately by default on ``result.stderr``.
    return CliRunner()


# ---------------------------------------------------------------------------
# End-to-end: exit code + hint substring for each category
# ---------------------------------------------------------------------------


def test_config_missing_exit_code_and_hint(runner: CliRunner) -> None:
    cmd = _make_command(lambda: FileNotFoundError(errno.ENOENT, "no such file", "bernstein.yaml"))
    result = runner.invoke(cmd, [])
    assert result.exit_code == exit_code_for(ErrorCategory.CONFIG_MISSING)
    assert result.exit_code == 65
    assert "No bernstein config" in result.stderr


def test_auth_failed_exit_code_and_hint(runner: CliRunner) -> None:
    cmd = _make_command(lambda: AdapterAuthError("401 unauthorized", adapter="claude", env_var="ANTHROPIC_API_KEY"))
    result = runner.invoke(cmd, [])
    assert result.exit_code == exit_code_for(ErrorCategory.AUTH_FAILED)
    assert result.exit_code == 77
    assert "claude" in result.stderr
    assert "ANTHROPIC_API_KEY" in result.stderr


def test_dependency_missing_exit_code_and_hint(runner: CliRunner) -> None:
    cmd = _make_command(
        lambda: AdapterBinaryNotFoundError(
            "binary not in PATH",
            adapter="aider",
            install_hint="pipx install aider",
        )
    )
    result = runner.invoke(cmd, [])
    assert result.exit_code == exit_code_for(ErrorCategory.DEPENDENCY_MISSING)
    assert result.exit_code == 69
    assert "aider" in result.stderr
    assert "pipx install aider" in result.stderr


def test_model_unreachable_exit_code_and_hint(runner: CliRunner) -> None:
    cmd = _make_command(lambda: ConnectionError("DNS failure"))
    result = runner.invoke(cmd, [])
    assert result.exit_code == exit_code_for(ErrorCategory.MODEL_UNREACHABLE)
    assert result.exit_code == 68
    assert "bernstein doctor extended" in result.stderr
    assert "--provider" not in result.stderr
    assert "BERNSTEIN_OFFLINE" not in result.stderr


def test_timeout_exit_code_and_hint(runner: CliRunner) -> None:
    cmd = _make_command(lambda: subprocess.TimeoutExpired(cmd=["claude"], timeout=30))
    result = runner.invoke(cmd, [])
    assert result.exit_code == exit_code_for(ErrorCategory.TIMEOUT)
    assert result.exit_code == 75
    assert "timed out" in result.stderr.lower()


def test_permission_denied_exit_code_and_hint(runner: CliRunner) -> None:
    cmd = _make_command(lambda: PermissionError(errno.EACCES, "denied", "/restricted/path"))
    result = runner.invoke(cmd, [])
    assert result.exit_code == exit_code_for(ErrorCategory.PERMISSION_DENIED)
    assert result.exit_code == 77
    # The path leaks through via the exception's ``filename`` attribute
    # into the hint context.
    assert "restricted" in result.stderr


def test_port_conflict_exit_code_and_hint(runner: CliRunner) -> None:
    cmd = _make_command(lambda: OSError(errno.EADDRINUSE, "address already in use"))
    result = runner.invoke(cmd, [])
    assert result.exit_code == exit_code_for(ErrorCategory.PORT_CONFLICT)
    assert result.exit_code == 75
    assert "Port" in result.stderr
    assert "lsof" in result.stderr


def test_unknown_exit_code_and_hint(runner: CliRunner) -> None:
    cmd = _make_command(lambda: ValueError("totally unexpected"))
    result = runner.invoke(cmd, [])
    assert result.exit_code == exit_code_for(ErrorCategory.UNKNOWN)
    assert result.exit_code == 70
    assert "Unhandled error" in result.stderr
    assert "github.com" in result.stderr


# ---------------------------------------------------------------------------
# Verbose-mode toggle: traceback only shows with --verbose
# ---------------------------------------------------------------------------


def test_verbose_flag_shows_traceback(runner: CliRunner) -> None:
    cmd = _make_command(lambda: RuntimeError("boom-verbose-trace"))
    result = runner.invoke(cmd, ["--verbose"])
    assert result.exit_code == 70
    # Rich's traceback rendering includes the exception class name.
    assert "RuntimeError" in result.stderr
    assert "boom-verbose-trace" in result.stderr


def test_default_mode_hides_traceback(runner: CliRunner) -> None:
    cmd = _make_command(lambda: RuntimeError("boom-default-trace"))
    result = runner.invoke(cmd, [])
    assert result.exit_code == 70
    # The hint must be present; the traceback class label must not.
    assert "Unhandled error" in result.stderr
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# Typed first-run error round trip via CLI
# ---------------------------------------------------------------------------


def test_typed_first_run_error_uses_attached_category(runner: CliRunner) -> None:
    cmd = _make_command(
        lambda: BernsteinFirstRunError(
            "explicit port conflict",
            category=ErrorCategory.PORT_CONFLICT,
            context={"port": 9090},
        )
    )
    result = runner.invoke(cmd, [])
    assert result.exit_code == exit_code_for(ErrorCategory.PORT_CONFLICT)
    assert "9090" in result.stderr


def test_socket_timeout_classified_as_timeout(runner: CliRunner) -> None:
    cmd = _make_command(lambda: TimeoutError("read deadline"))
    result = runner.invoke(cmd, [])
    assert result.exit_code == exit_code_for(ErrorCategory.TIMEOUT)


def test_usage_error_is_not_categorised(runner: CliRunner) -> None:
    """``click.UsageError`` retains Click's own pretty handling."""
    cmd = _make_command(lambda: click.UsageError("missing arg"))
    result = runner.invoke(cmd, [])
    # Click's standard usage-error exit code is 2.
    assert result.exit_code == 2
    assert "missing arg" in result.stderr.lower() or "missing arg" in (result.output or "").lower()


def test_systemexit_passes_through(runner: CliRunner) -> None:
    """An explicit ``SystemExit`` raised inside the body is honoured verbatim."""
    cmd = _make_command(lambda: SystemExit(42))
    result = runner.invoke(cmd, [])
    assert result.exit_code == 42
