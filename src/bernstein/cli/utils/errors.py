"""Structured error reporting for Bernstein CLI.

All user-facing errors follow the what/why/fix pattern:
  Error: <what went wrong>
    Reason: <why it happened>
    Fix: <how to resolve it>

Exit codes are standardised via :class:`ExitCode`:
  0 = success, 1 = general, 2 = usage, 3 = config, 4 = adapter, 5 = auth.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from rich.console import Console

console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Standardised exit codes (CLI-002)
# ---------------------------------------------------------------------------


class ExitCode(IntEnum):
    """Standardised CLI exit codes.

    Attributes:
        SUCCESS: Command completed successfully.
        GENERAL: Unspecified runtime failure.
        USAGE: Bad CLI usage (missing args, invalid flags).
        CONFIG: Configuration error (bad YAML, missing seed file).
        ADAPTER: Adapter/agent spawn or communication error.
        AUTH: Authentication/authorization failure.
    """

    SUCCESS = 0
    GENERAL = 1
    USAGE = 2
    CONFIG = 3
    ADAPTER = 4
    AUTH = 5


@dataclass
class BernsteinError(Exception):
    """Structured CLI error with what/why/fix guidance.

    Attributes:
        what: Short description of what failed (e.g. "Task server failed to start").
        why: Root cause explanation (e.g. "Port already in use").
        fix: Actionable fix instructions (e.g. "Run 'bernstein stop' first").
        exit_code: Standardised exit code for this error.
    """

    what: str
    why: str
    fix: str
    exit_code: ExitCode = ExitCode.GENERAL

    def __str__(self) -> str:
        return f"{self.what}\n  Reason: {self.why}\n  Fix: {self.fix}"

    def print(self) -> None:
        """Print the structured error to stderr using Rich.

        Also prints actionable next-step suggestions from the
        error_suggestions module when a known pattern is matched.
        """
        from bernstein.cli.error_suggestions import suggest_and_format

        console.print(f"[bold red]Error:[/bold red] {self.what}")
        console.print(f"  [yellow]Reason:[/yellow] {self.why}")
        console.print(f"  [green]Fix:[/green] {self.fix}")
        # Append auto-detected suggestion if different from the explicit fix
        extra = suggest_and_format(f"{self.what} {self.why}")
        if extra and extra.strip() not in self.fix:
            console.print(f"[dim]{extra}[/dim]")


def port_in_use(port: int) -> BernsteinError:
    """Return a BernsteinError for port-already-in-use failures."""
    return BernsteinError(
        what=f"Task server failed to start on port {port}",
        why="Port already in use by another process",
        fix=f"Run 'bernstein stop' first, or use --port {port + 1}",
        exit_code=ExitCode.GENERAL,
    )


def server_unreachable() -> BernsteinError:
    """Return a BernsteinError when the task server cannot be reached."""
    return BernsteinError(
        what="Cannot reach the Bernstein task server",
        why="No server is listening on port 8052",
        fix="Run 'bernstein' to start, or check 'bernstein doctor' for diagnostics",
        exit_code=ExitCode.GENERAL,
    )


def no_seed_or_goal() -> BernsteinError:
    """Return a BernsteinError when neither seed file nor goal is provided."""
    return BernsteinError(
        what="No goal or seed file found",
        why="Bernstein needs a goal to work from",
        fix="Run 'bernstein -g \"Your goal\"' for a quick start, or create bernstein.yaml",
        exit_code=ExitCode.CONFIG,
    )


def missing_api_key(adapter: str, env_var: str) -> BernsteinError:
    """Return a BernsteinError for a missing API key."""
    return BernsteinError(
        what=f"{adapter} adapter requires an API key",
        why=f"Environment variable {env_var} is not set",
        fix=f"export {env_var}=your-api-key",
        exit_code=ExitCode.AUTH,
    )


def bootstrap_failed(exc: Exception) -> BernsteinError:
    """Return a BernsteinError for a bootstrap/startup failure."""
    return BernsteinError(
        what="Bootstrap failed",
        why=str(exc),
        fix="Check .sdd/runtime/server.log for details, or run 'bernstein doctor'",
        exit_code=ExitCode.GENERAL,
    )


def seed_parse_error(exc: Exception) -> BernsteinError:
    """Return a BernsteinError for a seed file parsing failure."""
    return BernsteinError(
        what="Cannot parse seed file",
        why=str(exc),
        fix="Check bernstein.yaml syntax — see 'bernstein help-all' for format",
        exit_code=ExitCode.CONFIG,
    )


def server_error(exc: Exception) -> BernsteinError:
    """Return a BernsteinError for a task server communication failure."""
    return BernsteinError(
        what="Task server error",
        why=str(exc),
        fix="Check if the server is running with 'bernstein status', or restart with 'bernstein stop && bernstein'",
        exit_code=ExitCode.GENERAL,
    )


def no_cli_agent_found() -> BernsteinError:
    """Return a BernsteinError when no CLI agent binary is found in PATH."""
    return BernsteinError(
        what="No supported CLI agent found in PATH",
        why="Bernstein requires at least one CLI agent to be installed",
        fix=(
            "Install one of:\n"
            "    Claude Code  https://claude.ai/code\n"
            "    Codex CLI    https://github.com/openai/codex-cli\n"
            "    Gemini CLI   https://github.com/google-gemini/gemini-cli"
        ),
        exit_code=ExitCode.ADAPTER,
    )


def no_seed_file(filename: str = "bernstein.yaml") -> BernsteinError:
    """Return a BernsteinError when a seed file cannot be found."""
    return BernsteinError(
        what=f"No {filename} found",
        why="Bernstein needs a seed file or a --goal to work from",
        fix=f"Create {filename} or run 'bernstein -g \"your goal\"'",
        exit_code=ExitCode.CONFIG,
    )


def no_replay_tasks() -> BernsteinError:
    """Return a BernsteinError when a replay trace has no task IDs."""
    return BernsteinError(
        what="No task IDs found in trace",
        why="Cannot replay without tasks to re-submit",
        fix="Ensure the trace file was recorded from a valid run",
        exit_code=ExitCode.USAGE,
    )


# ---------------------------------------------------------------------------
# Convenience: handle + exit with correct code
# ---------------------------------------------------------------------------


def handle_cli_error(error: BernsteinError) -> SystemExit:
    """Print a structured error and return a ``SystemExit`` with the correct code.

    Usage::

        raise handle_cli_error(some_error)

    Args:
        error: The structured error to display.

    Returns:
        ``SystemExit`` with the error's exit code.
    """
    error.print()
    return SystemExit(error.exit_code)


def handle_unexpected_error(exc: Exception) -> SystemExit:
    """Print an unstructured exception with auto-detected suggestions and return SystemExit(1).

    Args:
        exc: Any exception caught in a CLI command.

    Returns:
        ``SystemExit(1)`` after printing the error.
    """
    from bernstein.cli.error_suggestions import suggest_and_format

    console.print(f"[bold red]Error:[/bold red] {exc}")
    extra = suggest_and_format(str(exc))
    if extra:
        console.print(f"[dim]{extra}[/dim]")
    return SystemExit(ExitCode.GENERAL)
