"""Structured error reporting for Bernstein CLI.

All user-facing errors follow the what/why/fix pattern:
  Error: <what went wrong>
    Reason: <why it happened>
    Fix: <how to resolve it>
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console

console = Console(stderr=True)


@dataclass
class BernsteinError(Exception):
    """Structured CLI error with what/why/fix guidance.

    Attributes:
        what: Short description of what failed (e.g. "Task server failed to start").
        why: Root cause explanation (e.g. "Port already in use").
        fix: Actionable fix instructions (e.g. "Run 'bernstein stop' first").
    """

    what: str
    why: str
    fix: str

    def __str__(self) -> str:
        return f"{self.what}\n  Reason: {self.why}\n  Fix: {self.fix}"

    def print(self) -> None:
        """Print the structured error to stderr using Rich."""
        console.print(f"[bold red]Error:[/bold red] {self.what}")
        console.print(f"  [yellow]Reason:[/yellow] {self.why}")
        console.print(f"  [green]Fix:[/green] {self.fix}")


def port_in_use(port: int) -> BernsteinError:
    """Return a BernsteinError for port-already-in-use failures."""
    return BernsteinError(
        what=f"Task server failed to start on port {port}",
        why="Port already in use by another process",
        fix=f"Run 'bernstein stop' first, or use --port {port + 1}",
    )


def server_unreachable() -> BernsteinError:
    """Return a BernsteinError when the task server cannot be reached."""
    return BernsteinError(
        what="Cannot reach the Bernstein task server",
        why="No server is listening on port 8052",
        fix="Run 'bernstein' to start, or check 'bernstein doctor' for diagnostics",
    )


def no_seed_or_goal() -> BernsteinError:
    """Return a BernsteinError when neither seed file nor goal is provided."""
    return BernsteinError(
        what="No goal or seed file found",
        why="Bernstein needs a goal to work from",
        fix=("Run 'bernstein -g \"Your goal\"' for a quick start, or create bernstein.yaml"),
    )


def missing_api_key(adapter: str, env_var: str) -> BernsteinError:
    """Return a BernsteinError for a missing API key."""
    return BernsteinError(
        what=f"{adapter} adapter requires an API key",
        why=f"Environment variable {env_var} is not set",
        fix=f"export {env_var}=your-api-key",
    )


def bootstrap_failed(exc: Exception) -> BernsteinError:
    """Return a BernsteinError for a bootstrap/startup failure."""
    return BernsteinError(
        what="Bootstrap failed",
        why=str(exc),
        fix="Check .sdd/runtime/server.log for details, or run 'bernstein doctor'",
    )


def seed_parse_error(exc: Exception) -> BernsteinError:
    """Return a BernsteinError for a seed file parsing failure."""
    return BernsteinError(
        what="Cannot parse seed file",
        why=str(exc),
        fix="Check bernstein.yaml syntax — see 'bernstein help-all' for format",
    )


def server_error(exc: Exception) -> BernsteinError:
    """Return a BernsteinError for a task server communication failure."""
    return BernsteinError(
        what="Task server error",
        why=str(exc),
        fix="Check if the server is running with 'bernstein status', or restart with 'bernstein stop && bernstein'",
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
    )


def no_seed_file(filename: str = "bernstein.yaml") -> BernsteinError:
    """Return a BernsteinError when a seed file cannot be found."""
    return BernsteinError(
        what=f"No {filename} found",
        why="Bernstein needs a seed file or a --goal to work from",
        fix=f"Create {filename} or run 'bernstein -g \"your goal\"'",
    )


def no_replay_tasks() -> BernsteinError:
    """Return a BernsteinError when a replay trace has no task IDs."""
    return BernsteinError(
        what="No task IDs found in trace",
        why="Cannot replay without tasks to re-submit",
        fix="Ensure the trace file was recorded from a valid run",
    )
