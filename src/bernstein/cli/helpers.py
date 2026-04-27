"""Shared constants, helpers, and utilities for Bernstein CLI modules."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console

from bernstein.core.platform_compat import kill_process, kill_process_group
from bernstein.core.process_utils import is_process_alive as _shared_is_process_alive

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER_URL = os.environ.get("BERNSTEIN_SERVER_URL", "http://localhost:8052")
SDD_DIRS = [
    ".sdd",
    ".sdd/backlog",
    ".sdd/backlog/open",
    ".sdd/backlog/done",
    ".sdd/agents",
    ".sdd/runtime",
    ".sdd/docs",
    ".sdd/decisions",
    ".sdd/audit",
    ".sdd/audit/merkle",
    ".sdd/chat",
]
SDD_PID_SERVER = ".sdd/runtime/server.pid"
SDD_PID_SPAWNER = ".sdd/runtime/spawner.pid"
SDD_PID_WATCHDOG = ".sdd/runtime/watchdog.pid"

# Use ASCII-safe banner on Windows to avoid cp1252 encoding issues
if sys.platform == "win32":
    BANNER = """\
+----------------------------------+
|  Bernstein - Agent Orchestra     |
+----------------------------------+"""
else:
    BANNER = """\
\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
\u2551  \U0001f3bc Bernstein \u2014 Agent Orchestra  \u2551
\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d"""

# Task status -> Rich color
STATUS_COLORS: dict[str, str] = {
    "open": "white",
    "claimed": "cyan",
    "in_progress": "yellow",
    "done": "green",
    "failed": "red",
    "blocked": "magenta",
    "cancelled": "red",
}

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def print_banner() -> None:
    console.print(f"[blue]{BANNER}[/blue]")


def auth_headers() -> dict[str, str]:
    """Return Authorization header dict if BERNSTEIN_AUTH_TOKEN is set."""
    token = os.environ.get("BERNSTEIN_AUTH_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def server_get(path: str) -> dict[str, Any] | None:
    """GET from the task server.  Returns None if server is unreachable."""
    try:
        resp = httpx.get(f"{SERVER_URL}{path}", timeout=5.0, headers=auth_headers())
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    except httpx.ConnectError:
        return None
    except Exception as exc:
        console.print(f"[red]Server error:[/red] {exc}")
        return None


def server_post(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST to the task server.  Returns None if server is unreachable."""
    try:
        resp = httpx.post(f"{SERVER_URL}{path}", json=payload, timeout=5.0, headers=auth_headers())
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    except httpx.ConnectError:
        return None
    except Exception as exc:
        console.print(f"[red]Server error:[/red] {exc}")
        return None


def read_pid(path: str) -> int | None:
    p = Path(path)
    if p.exists():
        try:
            return int(p.read_text().strip())
        except ValueError:
            return None
    return None


def write_pid(path: str, pid: int) -> None:  # type: ignore[reportUnusedFunction]
    Path(path).write_text(str(pid))


def is_alive(pid: int) -> bool:
    return _shared_is_process_alive(pid)


def kill_pid(path: str, label: str) -> None:
    pid = read_pid(path)
    if pid is None:
        console.print(f"[dim]No PID file found for {label}.[/dim]")
        return
    if is_alive(pid):
        # Kill the entire process group so child processes (pytest, uv,
        # agent subprocesses) don't survive and leak memory.
        if kill_process_group(pid, sig=signal.SIGTERM):
            console.print(f"[green]Sent SIGTERM to {label} process group (PID {pid}).[/green]")
        elif kill_process(pid, signal.SIGTERM):
            console.print(f"[green]Sent SIGTERM to {label} (PID {pid}).[/green]")
        else:
            console.print(f"[yellow]Could not terminate {label} (PID {pid}).[/yellow]")
    else:
        console.print(f"[dim]{label} (PID {pid}) was not running.[/dim]")
    Path(path).unlink(missing_ok=True)


def wait_for_death(pid: int, timeout: float = 2.0) -> bool:
    """Poll until *pid* is no longer alive, up to *timeout* seconds.

    Returns True if the process died, False if it's still alive.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.05)
    return not is_alive(pid)


def sigkill_pid(pid: int) -> bool:
    """Send SIGKILL to *pid* and its process group, then wait for death.

    Returns True if the process was killed (or was already dead).
    """
    if not is_alive(pid):
        return True
    if not kill_process_group(pid, sig=9):
        kill_process(pid, sig=9)
    return wait_for_death(pid)


def kill_pid_hard(path: str, label: str) -> None:
    """Kill a process by PID file using SIGKILL (no grace period).

    Sends SIGKILL to the entire process group, waits for the process to
    die, then removes the PID file.  Unlike :func:`kill_pid` which
    sends SIGTERM.

    Args:
        path: Path to the PID file.
        label: Human-readable label for log messages.
    """
    pid = read_pid(path)
    if pid is None:
        return
    if is_alive(pid):
        killed = sigkill_pid(pid)
        if killed:
            console.print(f"[red]Killed {label} (PID {pid}) with SIGKILL.[/red]")
        else:
            console.print(f"[yellow]{label} (PID {pid}) resisted SIGKILL — may need manual cleanup.[/yellow]")
    Path(path).unlink(missing_ok=True)


def print_dry_run_table(workdir: Path) -> None:
    """Print a summary table of tasks that would be spawned in dry-run mode.

    Reads open backlog tasks directly from .sdd/backlog/open/ and renders
    a Rich table showing role, title, provider, model, and effort.

    Args:
        workdir: Project root directory.
    """
    from rich.table import Table

    from bernstein.core.models import Complexity, Scope, Task
    from bernstein.core.router import TierAwareRouter, load_providers_from_yaml
    from bernstein.core.sync import BacklogTask, parse_backlog_file

    open_dir = workdir / ".sdd" / "backlog" / "open"
    issues_dir = workdir / ".sdd" / "backlog" / "issues"
    files: list[Path] = []
    for src_dir in (open_dir, issues_dir):
        if src_dir.exists():
            for ext in ("*.md", "*.yaml", "*.yml"):
                files.extend(src_dir.glob(ext))
    files.sort()

    tasks: list[BacklogTask] = []
    seen: set[str] = set()
    for backlog_file in files:
        if backlog_file.name in seen:
            continue
        seen.add(backlog_file.name)
        bt = parse_backlog_file(backlog_file)
        if bt is not None:
            tasks.append(bt)

    console.print("\n[bold cyan][DRY RUN] Planned task spawns:[/bold cyan]")

    if not tasks:
        console.print("[dim]No open tasks found in backlog.[/dim]")
        return

    # Initialize router and load providers
    router = TierAwareRouter()
    providers_yaml = workdir / ".sdd" / "config" / "providers.yaml"
    if providers_yaml.exists():
        load_providers_from_yaml(providers_yaml, router)

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Role", style="cyan")
    table.add_column("Title")
    table.add_column("Priority", justify="center")
    table.add_column("Provider", style="green")
    table.add_column("Model", style="dim")
    table.add_column("Effort", style="dim", justify="center")

    for bt in sorted(tasks, key=lambda t: t.priority):
        # Create a temporary Task object for the router
        t_obj = Task(
            id=bt.source_file,
            title=bt.title,
            description=bt.description,
            role=bt.role,
            priority=bt.priority,
            scope=Scope(bt.scope),
            complexity=Complexity(bt.complexity),
            model=None,  # BacklogTask doesn't expose model/effort yet
            effort=None,
        )

        try:
            decision = router.select_provider_for_task(t_obj)
            provider_display = decision.provider
            model_display = decision.model_config.model
            effort_display = decision.model_config.effort
        except Exception as e:
            provider_display = f"err: {e}"
            model_display = "auto"
            effort_display = "auto"

        table.add_row(
            bt.role,
            bt.title,
            str(bt.priority),
            provider_display,
            model_display,
            effort_display,
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(tasks)} task(s) — no agents were spawned.[/dim]")


_SEED_FILENAMES = ("bernstein.yaml", "bernstein.yml")


def find_seed_file() -> Path | None:
    """Look for a bernstein.yaml in the current directory.

    Returns:
        Path to the seed file if found, None otherwise.
    """
    for name in _SEED_FILENAMES:
        p = Path(name)
        if p.is_file():
            return p
    return None


def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    return is_alive(pid)


def is_json() -> bool:
    """Check if the current CLI context has the JSON flag enabled.

    This returns ``True`` when either ``--json`` or ``--output json`` was
    passed to the top-level CLI group or to the current subcommand.
    """
    try:
        ctx = click.get_current_context(silent=True)
        if ctx and ctx.obj:
            return bool(ctx.obj.get("JSON", False))
    except Exception:
        pass
    return False


def set_json_output(enabled: bool = True) -> None:
    """Programmatically enable JSON output mode in the current Click context.

    Args:
        enabled: Whether to enable or disable JSON output.
    """
    try:
        ctx = click.get_current_context(silent=True)
        if ctx:
            ctx.ensure_object(dict)
            ctx.obj["JSON"] = enabled
    except Exception:
        pass


def print_json(data: Any) -> None:
    """Print the given data as a JSON object to the console."""
    console.print_json(data=data)


def output_option(fn: Any) -> Any:
    """Click decorator that adds ``--output json`` as an alias for ``--json``.

    When ``--output json`` is specified the flag is stored in the Click
    context so that :func:`is_json` returns ``True`` for all downstream code.

    Usage::

        @click.command()
        @output_option
        def my_command(**kwargs: Any) -> None:
            if is_json():
                print_json({...})
    """
    import functools

    @click.option(
        "--output",
        "output_format",
        type=click.Choice(["json", "text"]),
        default=None,
        help="Output format: json for machine-readable, text for Rich (default).",
    )
    @functools.wraps(fn)
    def wrapper(*args: Any, output_format: str | None = None, **kwargs: Any) -> Any:
        if output_format == "json":
            set_json_output(True)
        return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Standardized color-coded output helpers (p1-0007)
# ---------------------------------------------------------------------------


def print_success(message: str) -> None:
    """Print a success message in green with a check mark prefix."""
    console.print(f"[green]✓[/green] {message}")


def print_error(message: str) -> None:
    """Print an error message in red with an x prefix."""
    console.print(f"[red]✗[/red] {message}")


def print_warning(message: str) -> None:
    """Print a warning message in yellow with a ! prefix."""
    console.print(f"[yellow]![/yellow] {message}")


def print_info(message: str) -> None:
    """Print an informational message in cyan with an → prefix."""
    console.print(f"[cyan]→[/cyan] {message}")
