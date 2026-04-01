"""Shared constants, helpers, and utilities for Bernstein CLI modules."""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

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
]
SDD_PID_SERVER = ".sdd/runtime/server.pid"
SDD_PID_SPAWNER = ".sdd/runtime/spawner.pid"
SDD_PID_WATCHDOG = ".sdd/runtime/watchdog.pid"

BANNER = """\
╔══════════════════════════════════╗
║  🎼 Bernstein — Agent Orchestra  ║
╚══════════════════════════════════╝"""

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
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_pid(path: str, label: str) -> None:
    pid = read_pid(path)
    if pid is None:
        console.print(f"[dim]No PID file found for {label}.[/dim]")
        return
    if is_alive(pid):
        try:
            # Kill the entire process group so child processes (pytest, uv,
            # agent subprocesses) don't survive and leak memory.
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                console.print(f"[green]Sent SIGTERM to {label} process group (PID {pid}, PGID {pgid}).[/green]")
            except (OSError, ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
                console.print(f"[green]Sent SIGTERM to {label} (PID {pid}).[/green]")
        except OSError as exc:
            console.print(f"[yellow]Could not terminate {label} (PID {pid}): {exc}[/yellow]")
    else:
        console.print(f"[dim]{label} (PID {pid}) was not running.[/dim]")
    Path(path).unlink(missing_ok=True)


def kill_pid_hard(path: str, label: str) -> None:
    """Kill a process by PID file using SIGKILL (no grace period).

    Unlike :func:`kill_pid` which sends SIGTERM, this sends SIGKILL to
    the entire process group for an immediate, non-catchable kill.

    Args:
        path: Path to the PID file.
        label: Human-readable label for log messages.
    """
    pid = read_pid(path)
    if pid is None:
        return
    if is_alive(pid):
        try:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            console.print(f"[red]Killed {label} (PID {pid}) with SIGKILL.[/red]")
        except OSError:
            pass
    Path(path).unlink(missing_ok=True)


def print_dry_run_table(workdir: Path) -> None:
    """Print a summary table of tasks that would be spawned in dry-run mode.

    Reads open backlog tasks directly from .sdd/backlog/open/ and renders
    a Rich table showing role, title, model, effort, priority, and scope.

    Args:
        workdir: Project root directory.
    """
    from rich.table import Table

    from bernstein.core.sync import BacklogTask, parse_backlog_file

    backlog_dir = workdir / ".sdd" / "backlog" / "open"
    tasks: list[BacklogTask] = []
    if backlog_dir.exists():
        for md_file in sorted(backlog_dir.glob("*.yaml")):
            bt = parse_backlog_file(md_file)
            if bt is not None:
                tasks.append(bt)

    console.print("\n[bold cyan][DRY RUN] Planned task spawns:[/bold cyan]")

    if not tasks:
        console.print("[dim]No open tasks found in backlog.[/dim]")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Role", style="cyan")
    table.add_column("Title")
    table.add_column("Priority", justify="center")
    table.add_column("Scope", justify="center")
    table.add_column("Complexity", justify="center")
    table.add_column("Model", style="dim", justify="center")
    table.add_column("Effort", style="dim", justify="center")

    for bt in sorted(tasks, key=lambda t: t.priority):
        table.add_row(
            bt.role,
            bt.title,
            str(bt.priority),
            bt.scope,
            bt.complexity,
            "auto",
            "auto",
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
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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
