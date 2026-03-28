"""CLI entry point for Bernstein -- multi-agent orchestration."""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import httpx
from rich.console import Console

if TYPE_CHECKING:
    from rich.table import Table
    from rich.text import Text

from bernstein.cli.cost import cost_cmd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER_URL = "http://localhost:8052"
SDD_DIRS = [
    ".sdd",
    ".sdd/backlog",
    ".sdd/backlog/open",
    ".sdd/backlog/done",
    ".sdd/agents",
    ".sdd/runtime",
    ".sdd/docs",
    ".sdd/decisions",
]
SDD_PID_SERVER = ".sdd/runtime/server.pid"
SDD_PID_SPAWNER = ".sdd/runtime/spawner.pid"
SDD_PID_WATCHDOG = ".sdd/runtime/watchdog.pid"

BANNER = """\
╔══════════════════════════════════╗
║  🎼 Bernstein — Agent Orchestra  ║
╚══════════════════════════════════╝"""

# Task status → Rich color
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


def _print_banner() -> None:
    from rich.panel import Panel

    console.print(Panel(BANNER, border_style="blue", expand=False))


def _print_dry_run_table(workdir: Path) -> None:
    """Print a summary table of tasks that would be spawned in dry-run mode.

    Reads open backlog tasks directly from .sdd/backlog/open/ and renders
    a Rich table showing role, title, model, effort, priority, and scope.

    Args:
        workdir: Project root directory.
    """
    from rich.table import Table

    from bernstein.core.sync import parse_backlog_file

    backlog_dir = workdir / ".sdd" / "backlog" / "open"
    tasks = []
    if backlog_dir.exists():
        for md_file in sorted(backlog_dir.glob("*.md")):
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


def _server_get(path: str) -> dict[str, Any] | None:
    """GET from the task server.  Returns None if server is unreachable."""
    try:
        resp = httpx.get(f"{SERVER_URL}{path}", timeout=5.0)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    except httpx.ConnectError:
        return None
    except Exception as exc:
        console.print(f"[red]Server error:[/red] {exc}")
        return None


def _server_post(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST to the task server.  Returns None if server is unreachable."""
    try:
        resp = httpx.post(f"{SERVER_URL}{path}", json=payload, timeout=5.0)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    except httpx.ConnectError:
        return None
    except Exception as exc:
        console.print(f"[red]Server error:[/red] {exc}")
        return None


def _read_pid(path: str) -> int | None:
    p = Path(path)
    if p.exists():
        try:
            return int(p.read_text().strip())
        except ValueError:
            return None
    return None


def _write_pid(path: str, pid: int) -> None:
    Path(path).write_text(str(pid))


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_pid(path: str, label: str) -> None:
    pid = _read_pid(path)
    if pid is None:
        console.print(f"[dim]No PID file found for {label}.[/dim]")
        return
    if _is_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            console.print(f"[green]Sent SIGTERM to {label} (PID {pid}).[/green]")
        except OSError as exc:
            console.print(f"[yellow]Could not terminate {label} (PID {pid}): {exc}[/yellow]")
    else:
        console.print(f"[dim]{label} (PID {pid}) was not running.[/dim]")
    Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


_SEED_FILENAMES = ("bernstein.yaml", "bernstein.yml")


def _find_seed_file() -> Path | None:
    """Look for a bernstein.yaml in the current directory.

    Returns:
        Path to the seed file if found, None otherwise.
    """
    for name in _SEED_FILENAMES:
        p = Path(name)
        if p.is_file():
            return p
    return None


@click.group(invoke_without_command=True)
@click.version_option(package_name="bernstein")
@click.option("--goal", "-g", default=None, help="Inline goal (no seed file needed).")
@click.option("--evolve", "-e", is_flag=True, default=False, help="Continuous self-improvement mode.")
@click.option("--max-cycles", default=0, help="Stop after N evolve cycles (0=unlimited).")
@click.option("--budget", default=0.0, help="Stop after $N spent (0=unlimited).")
@click.option("--interval", default=300, help="Seconds between evolve cycles (default 5min).")
@click.option("--headless", is_flag=True, default=False, help="Run without dashboard (for overnight/CI).")
@click.option("--dry-run", is_flag=True, default=False, help="Preview task plan without spawning agents.")
@click.pass_context
def cli(
    ctx: click.Context, goal: str | None, evolve: bool, max_cycles: int,
    budget: float, interval: int, headless: bool, dry_run: bool,
) -> None:
    """Bernstein — multi-agent orchestration for CLI coding agents.

    \b
    Usage:
      bernstein                             Start from seed file or backlog
      bernstein -g "Build auth with JWT"    Start with inline goal
      bernstein --evolve                    Continuous self-improvement
      bernstein --dry-run                   Preview task plan (no agents spawned)
      bernstein stop                        Stop everything

    HTTP API on port 8052 for programmatic access.
    """
    if ctx.invoked_subcommand is not None:
        return

    _print_banner()

    seed_path = _find_seed_file()
    workdir = Path.cwd()
    port = 8052

    if dry_run:
        _print_dry_run_table(workdir)
        return

    # Check if already running
    server_pid_path = Path(SDD_PID_SERVER)
    server_pid = _read_pid(str(server_pid_path))
    already_running = server_pid is not None and _is_alive(server_pid)

    if not already_running:
        # Write run_config.json so the orchestrator subprocess can read budget_usd
        if budget > 0:
            import json as _json
            runtime_dir = workdir / ".sdd" / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "run_config.json").write_text(
                _json.dumps({"budget_usd": budget})
            )

        if goal is not None:
            # Inline goal — no config files needed
            console.print(f"Goal: [bold]{goal}[/bold]")
            from bernstein.core.bootstrap import bootstrap_from_goal
            try:
                bootstrap_from_goal(goal, workdir=workdir, port=port)
            except RuntimeError as exc:
                console.print(f"[red]Error:[/red] {exc}")
                raise SystemExit(1) from exc
        elif seed_path is not None:
            console.print(f"Using: [bold]{seed_path.name}[/bold]")
            from bernstein.core.bootstrap import bootstrap_from_seed
            from bernstein.core.seed import SeedError
            try:
                bootstrap_from_seed(seed_path, workdir=workdir, port=port)
            except (SeedError, RuntimeError) as exc:
                console.print(f"[red]Error:[/red] {exc}")
                raise SystemExit(1) from exc
        else:
            # No seed file, no goal — check if backlog has tasks
            backlog_dir = workdir / ".sdd" / "backlog" / "open"
            has_backlog = backlog_dir.exists() and any(backlog_dir.glob("*.md"))
            if has_backlog:
                task_count = sum(1 for _ in backlog_dir.glob("*.md"))
                console.print(f"[dim]No seed file — loading {task_count} tasks from backlog[/dim]")
                from bernstein.core.bootstrap import bootstrap_from_goal
                try:
                    bootstrap_from_goal("Execute backlog tasks", workdir=workdir, port=port)
                except RuntimeError as exc:
                    console.print(f"[red]Error:[/red] {exc}")
                    raise SystemExit(1) from exc
            else:
                console.print(
                    'No bernstein.yaml or backlog tasks found.\n\n'
                    '[bold]Quick start:[/bold]\n'
                    '  bernstein -g "Build a REST API with auth"\n\n'
                    'Or create a bernstein.yaml / add .md tasks to .sdd/backlog/open/\n'
                )
                return
    else:
        console.print("[dim]Already running.[/dim]")

    # Write evolve config so the orchestrator can read it
    if evolve:
        import json as _json
        evolve_config = {
            "enabled": True,
            "max_cycles": max_cycles,
            "budget_usd": budget,
            "interval_s": interval,
        }
        evolve_path = workdir / ".sdd" / "runtime" / "evolve.json"
        evolve_path.parent.mkdir(parents=True, exist_ok=True)
        evolve_path.write_text(_json.dumps(evolve_config))
        console.print(
            f"[bold cyan]Evolve mode ON[/bold cyan] "
            f"(interval={interval}s"
            f"{f', max_cycles={max_cycles}' if max_cycles else ''}"
            f"{f', budget=${budget:.2f}' if budget else ''})"
        )

    if headless:
        console.print("[bold green]Running headless.[/bold green] Check .sdd/runtime/ for logs.")
        return

    # Show live dashboard (blocks until Ctrl+C / q)
    from bernstein.cli.dashboard import run_dashboard
    run_dashboard()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@cli.command("overture", hidden=True)
@click.option(
    "--dir",
    "target_dir",
    default=".",
    show_default=True,
    help="Directory to initialise (default: current directory).",
)
def init(target_dir: str) -> None:
    """Init workspace — create .sdd/ structure."""
    _print_banner()
    root = Path(target_dir).resolve()
    console.print(f"Initialising Bernstein workspace in [bold]{root}[/bold]")

    for d in SDD_DIRS:
        p = root / d
        p.mkdir(parents=True, exist_ok=True)

    # Write a minimal default config
    config_path = root / ".sdd" / "config.yaml"
    if not config_path.exists():
        config_path.write_text(
            "# Bernstein workspace config\n"
            "server_port: 8052\n"
            "max_workers: 6\n"
            "default_model: sonnet\n"
            "default_effort: high\n"
        )
        console.print(f"[green]Created[/green] {config_path.relative_to(root)}")

    # Write a .gitignore for the runtime dir
    gi_path = root / ".sdd" / "runtime" / ".gitignore"
    if not gi_path.exists():
        gi_path.write_text("*.pid\n*.log\n")

    console.print("[green]✓[/green] Workspace ready. Run [bold]bernstein start[/bold] to begin.")


# ---------------------------------------------------------------------------
# run  (the "one command" Seed UX)
# ---------------------------------------------------------------------------


@cli.command("conduct", hidden=True)
@click.option(
    "--goal",
    default=None,
    help="Inline goal (skips bernstein.yaml).",
)
@click.option(
    "--seed",
    "seed_file",
    default=None,
    help="Path to a custom seed YAML file (default: bernstein.yaml).",
)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Port for the task server.",
)
def run(goal: str | None, seed_file: str | None, port: int) -> None:
    """Parse seed, init workspace, start server, launch agents.

    \b
      bernstein conduct                     # reads bernstein.yaml
      bernstein conduct --goal "Build X"    # inline goal
      bernstein conduct --seed custom.yaml  # custom seed file
    """
    _print_banner()

    from bernstein.core.bootstrap import bootstrap_from_goal, bootstrap_from_seed
    from bernstein.core.seed import SeedError

    workdir = Path.cwd()

    if goal is not None:
        # Inline goal mode -- no YAML needed
        try:
            bootstrap_from_goal(goal=goal, workdir=workdir, port=port)
        except RuntimeError as exc:
            console.print(f"[red]Bootstrap error:[/red] {exc}")
            raise SystemExit(1) from exc
        return

    # Seed file mode
    if seed_file is not None:
        path = Path(seed_file)
    else:
        found = _find_seed_file()
        if found is not None:
            path = found
        else:
            console.print(
                "[yellow]No seed file found and no --goal given.[/yellow]\n"
                "Create bernstein.yaml or pass [bold]--goal 'your goal'[/bold]."
            )
            raise SystemExit(1)

    console.print(f"[dim]Using seed file:[/dim] {path}")
    try:
        bootstrap_from_seed(seed_path=path, workdir=workdir, port=port)
    except SeedError as exc:
        console.print(f"[red]Seed error:[/red] {exc}")
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        console.print(f"[red]Bootstrap error:[/red] {exc}")
        raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# start  (legacy, kept for backward compat)
# ---------------------------------------------------------------------------


@cli.command("downbeat", hidden=True)
@click.argument("goal", required=False, default=None)
@click.option(
    "--seed-file",
    default="bernstein.yaml",
    show_default=True,
    help="YAML seed file to read goal/tasks from (used when GOAL is not given).",
)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Port for the task server.",
)
def start(goal: str | None, seed_file: str, port: int) -> None:
    """Start server and spawn manager (legacy, use 'conduct')."""
    _print_banner()

    from bernstein.core.bootstrap import bootstrap_from_goal, bootstrap_from_seed
    from bernstein.core.seed import SeedError

    workdir = Path.cwd()

    if goal:
        try:
            bootstrap_from_goal(goal=goal, workdir=workdir, port=port)
        except RuntimeError as exc:
            console.print(f"[red]Bootstrap error:[/red] {exc}")
            raise SystemExit(1) from exc
    else:
        path = Path(seed_file)
        if not path.exists():
            console.print(
                "[yellow]No GOAL argument and no seed file found.[/yellow] "
                f"Pass a goal or create {seed_file}."
            )
            raise SystemExit(1)
        try:
            bootstrap_from_seed(seed_path=path, workdir=workdir, port=port)
        except SeedError as exc:
            console.print(f"[red]Seed error:[/red] {exc}")
            raise SystemExit(1) from exc
        except RuntimeError as exc:
            console.print(f"[red]Bootstrap error:[/red] {exc}")
            raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command("score", hidden=True)
def status() -> None:
    """Task summary, active agents, cost estimate."""
    _print_banner()

    data = _server_get("/status")
    if data is None:
        console.print(
            "[red]Cannot reach task server.[/red] "
            "Is Bernstein running? Try [bold]bernstein start[/bold]."
        )
        raise SystemExit(1)

    # ---- Task table ----
    from rich.table import Table

    tasks: list[dict[str, Any]] = data.get("tasks", [])
    task_table = Table(title="Tasks", show_lines=False, header_style="bold cyan")
    task_table.add_column("ID", style="dim", min_width=10)
    task_table.add_column("Title", min_width=30)
    task_table.add_column("Role", min_width=10)
    task_table.add_column("Status", min_width=14)
    task_table.add_column("Priority", justify="right")
    task_table.add_column("Agent", min_width=12)

    for t in tasks:
        raw_status = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        task_table.add_row(
            t.get("id", "—"),
            t.get("title", "—"),
            t.get("role", "—"),
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("priority", 2)),
            t.get("assigned_agent") or "[dim]—[/dim]",
        )

    console.print(task_table)

    # ---- Agent table ----
    agents: list[dict[str, Any]] = data.get("agents", [])
    if agents:
        agent_table = Table(title="Active Agents", show_lines=False, header_style="bold cyan")
        agent_table.add_column("ID", style="dim", min_width=12)
        agent_table.add_column("Role", min_width=10)
        agent_table.add_column("Status", min_width=10)
        agent_table.add_column("Model", min_width=10)
        agent_table.add_column("Tasks")

        for a in agents:
            raw_astatus = a.get("status", "idle")
            acolor = "yellow" if raw_astatus == "working" else "dim"
            agent_table.add_row(
                a.get("id", "—"),
                a.get("role", "—"),
                f"[{acolor}]{raw_astatus}[/{acolor}]",
                a.get("model", "—"),
                str(len(a.get("task_ids", []))),
            )
        console.print(agent_table)
    else:
        console.print("[dim]No active agents.[/dim]")

    # ---- Summary stats ----
    summary: dict[str, Any] = data.get("summary", {})
    total = summary.get("total", len(tasks))
    done = summary.get("done", sum(1 for t in tasks if t.get("status") == "done"))
    in_prog = summary.get("in_progress", sum(1 for t in tasks if t.get("status") == "in_progress"))
    failed = summary.get("failed", sum(1 for t in tasks if t.get("status") == "failed"))

    console.print(
        f"\n[bold]Tasks:[/bold] {total} total  "
        f"[green]{done} done[/green]  "
        f"[yellow]{in_prog} in progress[/yellow]  "
        f"[red]{failed} failed[/red]"
    )

    elapsed_s: int | None = data.get("elapsed_seconds")
    if elapsed_s is not None:
        minutes, secs = divmod(elapsed_s, 60)
        console.print(f"[bold]Elapsed:[/bold] {minutes}m {secs}s")

    # ---- Cost section ----
    total_cost_usd: float = data.get("total_cost_usd", 0.0)
    per_role: list[dict[str, Any]] = data.get("per_role", [])
    roles_with_cost = [r for r in per_role if r.get("cost_usd", 0.0) > 0.0]
    if total_cost_usd > 0.0 or roles_with_cost:
        console.print(f"\n[bold]Total spend:[/bold] [green]${total_cost_usd:.4f}[/green]")
        if roles_with_cost:
            cost_table = Table(title="Cost by Role", show_lines=False, header_style="bold cyan")
            cost_table.add_column("Role", min_width=12)
            cost_table.add_column("Tasks", justify="right")
            cost_table.add_column("Cost", justify="right")
            for r in sorted(roles_with_cost, key=lambda x: x.get("cost_usd", 0.0), reverse=True):
                role_tasks = r.get("done", 0) + r.get("failed", 0) + r.get("claimed", 0) + r.get("open", 0)
                cost_table.add_row(
                    r.get("role", "—"),
                    str(role_tasks),
                    f"${r.get('cost_usd', 0.0):.4f}",
                )
            console.print(cost_table)


# ---------------------------------------------------------------------------
# add-task
# ---------------------------------------------------------------------------


@cli.command("compose", hidden=True)
@click.argument("title")
@click.option("--role", default="backend", show_default=True, help="Agent role for this task.")
@click.option("--description", "-d", default="", help="Task description.")
@click.option(
    "--priority",
    type=click.IntRange(1, 3),
    default=2,
    show_default=True,
    help="1=critical, 2=normal, 3=nice-to-have.",
)
@click.option(
    "--scope",
    type=click.Choice(["small", "medium", "large"]),
    default="medium",
    show_default=True,
)
@click.option(
    "--complexity",
    type=click.Choice(["low", "medium", "high"]),
    default="medium",
    show_default=True,
)
@click.option("--depends-on", multiple=True, metavar="TASK_ID", help="Task IDs this depends on.")
def add_task(
    title: str,
    role: str,
    description: str,
    priority: int,
    scope: str,
    complexity: str,
    depends_on: tuple[str, ...],
) -> None:
    """Add a task to the running server.

    TITLE is the short task name.
    """
    payload: dict[str, Any] = {
        "title": title,
        "role": role,
        "description": description,
        "priority": priority,
        "scope": scope,
        "complexity": complexity,
        "depends_on": list(depends_on),
    }

    result = _server_post("/task", payload)
    if result is None:
        console.print(
            "[red]Cannot reach task server.[/red] "
            "Is Bernstein running? Try [bold]bernstein start[/bold]."
        )
        raise SystemExit(1)

    task_id = result.get("id", "?")
    console.print(
        f"[green]Task added:[/green] [bold]{task_id}[/bold] — {title} "
        f"([dim]role={role}, priority={priority}[/dim])"
    )


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@cli.command("sync", hidden=True)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Task server port.",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def sync(port: int, workdir: str) -> None:
    """Sync .sdd/backlog/open/*.md files with the task server.

    \b
    Creates server tasks for new backlog files not yet on the server.
    Moves .md files to backlog/done/ when their task is completed.
    """
    from bernstein.core.sync import sync_backlog_to_server

    root = Path(workdir).resolve()
    result = sync_backlog_to_server(root, server_url=f"http://127.0.0.1:{port}")

    if result.created:
        console.print(
            f"[green]Created {len(result.created)} task(s):[/green] "
            + ", ".join(result.created)
        )
    if result.skipped:
        console.print(
            f"[dim]Skipped {len(result.skipped)} file(s) already on server[/dim]"
        )
    if result.moved:
        console.print(
            f"[green]Moved {len(result.moved)} completed file(s) to backlog/done/:[/green] "
            + ", ".join(result.moved)
        )
    for err in result.errors:
        console.print(f"[red]Error:[/red] {err}")

    if not result.created and not result.moved and not result.errors:
        if result.skipped:
            console.print("[dim]All backlog files already synced.[/dim]")
        else:
            console.print("[dim]Nothing to sync — backlog/open/ is empty.[/dim]")


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@cli.command("stop")
@click.option(
    "--timeout",
    default=10,
    show_default=True,
    help="Seconds to wait for agents to finish before force-killing.",
)
def stop(timeout: int) -> None:
    """Gracefully stop all agents and the task server."""
    _print_banner()
    console.print("[bold]Stopping Bernstein…[/bold]\n")

    # 1. Ask the server to initiate graceful shutdown of all agents
    data = _server_post("/shutdown", {})
    if data is not None:
        console.print("[dim]Shutdown signal sent to task server.[/dim]")
    else:
        console.print("[dim]Task server not reachable — skipping graceful shutdown.[/dim]")

    # 2. Wait briefly for agents to wind down
    if timeout > 0:
        console.print(f"[dim]Waiting up to {timeout}s for agents…[/dim]")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status_data = _server_get("/status")
            if status_data is None:
                break
            active = [
                a
                for a in status_data.get("agents", [])
                if a.get("status") in {"working", "starting"}
            ]
            if not active:
                break
            time.sleep(1)

    # 3. Kill watchdog first so it doesn't restart things we're stopping
    _kill_pid(SDD_PID_WATCHDOG, "Watchdog")

    # 4. Kill spawner
    _kill_pid(SDD_PID_SPAWNER, "Spawner")

    # 5. Kill all spawned agents (they run in separate process groups)
    agents_json = Path(".sdd/runtime/agents.json")
    if agents_json.exists():
        try:
            import json as _json
            agent_data = _json.loads(agents_json.read_text())
            for agent in agent_data.get("agents", []):
                pid = agent.get("pid")
                if pid and _is_alive(pid):
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                        console.print(f"[dim]Killed agent {agent.get('id', '?')} (PID {pid})[/dim]")
                    except OSError:
                        pass
        except (OSError, ValueError):
            pass

    # 6. Kill server
    _kill_pid(SDD_PID_SERVER, "Task server")

    console.print("\n[green]Bernstein stopped.[/green]")


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.option("--reason", "-r", default="Cancelled by user", help="Cancellation reason")
def cancel(task_id: str, reason: str) -> None:
    """Cancel a running or queued task."""
    data = _server_post(f"/tasks/{task_id}/cancel", {"reason": reason})
    if data is None:
        console.print("[red]Server not reachable.[/red]")
        raise SystemExit(1)
    console.print(f"[green]Cancelled:[/green] {data['title']}")
    console.print(f"[dim]Status: {data['status']}[/dim]")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@cli.command("plan")
@click.option(
    "--export",
    "export_file",
    default=None,
    metavar="FILE",
    help="Write full task list as formatted JSON to FILE.",
)
@click.option(
    "--status",
    "status_filter",
    default=None,
    type=click.Choice(["open", "claimed", "in_progress", "done", "failed", "blocked", "cancelled"]),
    help="Filter tasks by status.",
)
def plan(export_file: str | None, status_filter: str | None) -> None:
    """Show task backlog as a table, or export to JSON.

    \b
      bernstein plan                          # show all tasks
      bernstein plan --status open            # show only open tasks
      bernstein plan --export plan.json       # export full backlog to JSON
    """
    from rich.table import Table

    path = "/tasks"
    if status_filter:
        path = f"/tasks?status={status_filter}"

    raw = _server_get(path)
    if raw is None:
        console.print(
            "[red]Cannot reach task server.[/red] "
            "Is Bernstein running? Try [bold]bernstein start[/bold]."
        )
        raise SystemExit(1)

    tasks: list[dict[str, Any]] = raw if isinstance(raw, list) else []

    if export_file:
        out = Path(export_file)
        out.write_text(json.dumps(tasks, indent=2))
        console.print(f"Exported {len(tasks)} tasks to {export_file}")
        return

    if not tasks:
        console.print("[dim]No tasks found.[/dim]")
        return

    table = Table(title="Task Backlog", show_lines=False, header_style="bold cyan")
    table.add_column("ID", style="dim", min_width=10)
    table.add_column("Status", min_width=12)
    table.add_column("Role", min_width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Depends On", min_width=12)
    table.add_column("Model", min_width=8)
    table.add_column("Effort", min_width=8)

    for t in tasks:
        raw_status = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        depends = ", ".join(d[:8] for d in t.get("depends_on", [])) or "—"
        table.add_row(
            t.get("id", "—")[:8],
            f"[{color}]{raw_status}[/{color}]",
            t.get("role", "—"),
            t.get("title", "—"),
            depends,
            t.get("model") or "—",
            t.get("effort") or "—",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# logs — tail agent output in real-time
# ---------------------------------------------------------------------------


def _find_agent_logs(runtime_dir: Path, agent_id: str | None) -> list[Path]:
    """Return agent log files from runtime_dir sorted by mtime, optionally filtered by agent_id."""
    if not runtime_dir.exists():
        return []
    log_list = [p for p in runtime_dir.glob("*.log") if p.name != "watchdog.log"]
    if agent_id:
        log_list = [p for p in log_list if agent_id in p.stem]
    return sorted(log_list, key=lambda p: p.stat().st_mtime)


@cli.command("logs")
@click.option("--follow", "-f", is_flag=True, default=False, help="Stream log output in real-time (like tail -f).")
@click.option("--agent", "-a", default=None, help="Filter by agent session ID (partial match).")
@click.option("--lines", "-n", default=50, show_default=True, help="Number of lines to show without --follow.")
@click.option("--runtime-dir", default=".sdd/runtime", show_default=True, hidden=True, help="Directory containing agent log files.")
def logs_cmd(follow: bool, agent: str | None, lines: int, runtime_dir: str) -> None:
    """Tail agent log output.

    Without --follow, prints the last N lines of the most recent agent log.
    With --follow (-f), streams new output in real-time until Ctrl+C.
    """
    rdir = Path(runtime_dir)
    log_files = _find_agent_logs(rdir, agent)

    if not log_files:
        suffix = f" matching '{agent}'" if agent else ""
        console.print(f"[yellow]No agent logs found{suffix} in {rdir}[/yellow]")
        raise SystemExit(1)

    log_path = log_files[-1]  # most recent
    console.print(f"[dim]Watching:[/dim] [bold]{log_path.name}[/bold]")

    if not follow:
        text = log_path.read_text(errors="replace")
        tail_lines = text.splitlines()[-lines:]
        console.print("\n".join(tail_lines) or "[dim](empty)[/dim]")
        return

    # --follow: print last N lines as context then stream new bytes
    try:
        existing = log_path.read_text(errors="replace")
        context = existing.splitlines()[-lines:]
        if context:
            console.print("\n".join(context))
        offset = log_path.stat().st_size
    except FileNotFoundError:
        offset = 0

    console.print("[dim]--- following (Ctrl+C to stop) ---[/dim]")
    try:
        while True:
            try:
                size = log_path.stat().st_size
            except FileNotFoundError:
                time.sleep(0.2)
                continue

            if size > offset:
                with log_path.open("rb") as fh:
                    fh.seek(offset)
                    new_bytes = fh.read(size - offset)
                offset = size
                console.print(new_bytes.decode(errors="replace"), end="")

            time.sleep(0.2)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")


@cli.command("notes", hidden=True)
@click.option("--lines", "-n", default=40, show_default=True, help="Number of tail lines to display.")
@click.option("--component", type=click.Choice(["server", "spawner"]), default="server", show_default=True)
def _notes_legacy(lines: int, component: str) -> None:
    """Tail server or spawner logs (legacy alias)."""
    log_path = Path(f".sdd/runtime/{component}.log")
    if not log_path.exists():
        console.print(f"[red]Log file not found:[/red] {log_path}")
        raise SystemExit(1)

    from rich.panel import Panel

    all_lines = log_path.read_text(errors="replace").splitlines()
    tail = all_lines[-lines:]
    console.print(
        Panel(
            "\n".join(tail) or "[dim](empty)[/dim]",
            title=f"[bold]{component}.log[/bold] (last {lines} lines)",
            border_style="dim",
        )
    )


# ---------------------------------------------------------------------------
# tasks  (alias for `bernstein status --tasks-only`)
# ---------------------------------------------------------------------------


@cli.command("parts", hidden=True)
@click.option(
    "--status-filter",
    "status_filter",
    default=None,
    type=click.Choice(["open", "claimed", "in_progress", "done", "failed", "blocked"]),
    help="Filter by task status.",
)
@click.option("--role", default=None, help="Filter by role.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def list_tasks(status_filter: str | None, role: str | None, as_json: bool) -> None:
    """List tasks with optional filters."""
    data = _server_get("/status")
    if data is None:
        console.print(
            "[red]Cannot reach task server.[/red] "
            "Is Bernstein running? Try [bold]bernstein start[/bold]."
        )
        raise SystemExit(1)

    tasks: list[dict[str, Any]] = data.get("tasks", [])

    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    if role:
        tasks = [t for t in tasks if t.get("role") == role]

    if as_json:
        console.print_json(json.dumps(tasks))
        return

    if not tasks:
        console.print("[dim]No tasks matching filters.[/dim]")
        return

    from rich.table import Table

    table = Table(show_lines=False, header_style="bold cyan")
    table.add_column("ID", style="dim", min_width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Role", min_width=10)
    table.add_column("Status", min_width=14)
    table.add_column("Priority", justify="right")

    for t in tasks:
        raw_status = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        table.add_row(
            t.get("id", "—"),
            t.get("title", "—"),
            t.get("role", "—"),
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("priority", 2)),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# live — module-level display helpers (extracted for testability)
# ---------------------------------------------------------------------------


def _build_agents_table(agents: list[dict[str, Any]]) -> Table:
    """Build a Rich Table showing active agents."""
    from rich.table import Table

    table = Table(
        title="Agents",
        show_lines=False,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Agent", min_width=18)
    table.add_column("Model", min_width=8)
    table.add_column("Status", min_width=10)
    table.add_column("Runtime", justify="right", min_width=7)
    table.add_column("Tasks", justify="right", min_width=5)

    for a in agents:
        raw = a.get("status", "idle")
        color = {"working": "yellow", "starting": "cyan", "dead": "red"}.get(raw, "green" if raw == "done" else "dim")
        runtime_s = a.get("runtime_s", 0)
        mins, secs = divmod(int(runtime_s), 60)
        runtime_str = f"{mins}:{secs:02d}" if runtime_s > 0 else "—"
        model = a.get("model") or "—"
        task_count = len(a.get("task_ids", []))
        table.add_row(
            f"[bold]{a.get('role', '?')}[/bold] [dim]{a.get('id', '—')[-8:]}[/dim]",
            model,
            f"[{color}]{raw}[/{color}]",
            runtime_str,
            str(task_count),
        )
    return table


def _build_events_table(tasks: list[dict[str, Any]]) -> Table:
    """Build a Rich Table showing tasks."""
    from rich.table import Table

    table = Table(
        title="Tasks",
        show_lines=False,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Status", min_width=9)
    table.add_column("Role", min_width=8)
    table.add_column("Title")

    for t in tasks:
        raw_status = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        icon = {"done": "+", "failed": "x", "claimed": ">", "open": " "}.get(raw_status, " ")
        table.add_row(
            f"[{color}]{icon} {raw_status}[/{color}]",
            t.get("role", "—"),
            t.get("title", "—"),
        )
    return table


def _build_stats_bar(summary: dict[str, Any]) -> Text:
    """Build a Rich Text stats bar from a summary dict."""
    from rich.text import Text

    total = summary.get("total", 0)
    done = summary.get("done", 0)
    in_prog = summary.get("in_progress", 0)
    failed = summary.get("failed", 0)
    elapsed_s = int(summary.get("elapsed_seconds", 0))
    mins, secs = divmod(elapsed_s, 60)

    bar = Text()
    bar.append(f"Tasks: {total}  ", style="bold")
    bar.append(f"done={done} ", style="green")
    bar.append(f"working={in_prog} ", style="yellow")
    bar.append(f"failed={failed}  ", style="red")
    # Progress bar
    if total > 0:
        pct = int(done / total * 100)
        filled = pct // 5
        bar.append(f"[{'=' * filled}{' ' * (20 - filled)}] {pct}%  ", style="bold green")
    bar.append(f"{mins}m{secs:02d}s", style="dim")
    return bar


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


@cli.command("live")
@click.option(
    "--interval",
    default=2.0,
    show_default=True,
    help="Polling interval in seconds.",
)
def live(interval: float) -> None:
    """Live dashboard: active agents, task events, and stats (Ctrl+C to exit)."""
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel

    _print_banner()

    def _fetch_dashboard() -> dict[str, Any]:
        """Fetch all data needed for the live dashboard."""
        status = _server_get("/status")
        if status is None:
            return {}
        tasks_raw = _server_get("/tasks")
        tasks = tasks_raw if isinstance(tasks_raw, list) else []

        # Read agent state from orchestrator's agents.json (written each tick)
        agents: list[dict[str, Any]] = []
        agents_json = Path(".sdd/runtime/agents.json")
        if agents_json.exists():
            try:
                import json as _json
                data = _json.loads(agents_json.read_text())
                agents = [a for a in data.get("agents", []) if a.get("status") != "dead"]
            except (OSError, ValueError):
                pass

        elapsed = time.time() - _live_start_ts

        return {
            "agents": agents,
            "tasks": tasks,
            "summary": {
                "total": status.get("total", 0),
                "done": status.get("done", 0),
                "in_progress": status.get("claimed", 0),
                "failed": status.get("failed", 0),
                "elapsed_seconds": elapsed,
            },
        }

    _live_start_ts = time.time()
    data: dict[str, Any] = {}

    def _render() -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="agents", ratio=3),
            Layout(name="events", ratio=4),
            Layout(name="stats", size=3),
        )
        agents = data.get("agents", [])
        tasks = data.get("tasks", [])
        summary = data.get("summary", {})
        layout["agents"].update(_build_agents_table(agents))
        layout["events"].update(_build_events_table(tasks))
        layout["stats"].update(Panel(_build_stats_bar(summary), border_style="blue"))
        return layout

    try:
        with Live(_render(), refresh_per_second=1, screen=True) as live_display:
            while True:
                data = _fetch_dashboard()
                tasks_list: list[dict[str, Any]] = data.get("tasks", [])
                new_map: dict[str, str] = {}
                for t in tasks_list:
                    new_map[t.get("id", "")] = t.get("status", "open")
                live_display.update(_render())
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    console.print("\n[dim]Live display stopped.[/dim]")


# ---------------------------------------------------------------------------
# benchmark
# ---------------------------------------------------------------------------


@cli.group("benchmark")
def benchmark_group() -> None:
    """Run the tiered golden benchmark suite."""


@benchmark_group.command("run")
@click.option(
    "--tier",
    type=click.Choice(["smoke", "capability", "stretch", "all"]),
    default="all",
    show_default=True,
    help="Which benchmark tier to run.",
)
@click.option(
    "--benchmarks-dir",
    default="tests/benchmarks",
    show_default=True,
    help="Root directory containing smoke/capability/stretch sub-dirs.",
)
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Persist results to .sdd/benchmarks/YYYY-MM-DD.jsonl.",
)
def benchmark_run(tier: str, benchmarks_dir: str, save: bool) -> None:
    """Run benchmark suite and report pass/fail per benchmark.

    \b
      bernstein benchmark run                  # run all tiers
      bernstein benchmark run --tier smoke     # smoke only
      bernstein benchmark run --tier stretch   # stretch only
    """
    from rich.table import Table

    from bernstein.evolution.benchmark import (
        run_all,
        run_selected,
        save_results,
    )

    bdir = Path(benchmarks_dir)
    if not bdir.exists():
        console.print(f"[red]Benchmarks directory not found:[/red] {bdir}")
        raise SystemExit(1)

    summary = run_all(bdir) if tier == "all" else run_selected(bdir, tier)  # type: ignore[arg-type]

    # ---- Results table ----
    table = Table(title=f"Benchmarks — tier={tier}", header_style="bold cyan", show_lines=False)
    table.add_column("ID", style="dim", min_width=14)
    table.add_column("Tier", min_width=12)
    table.add_column("Goal", min_width=40)
    table.add_column("Result", min_width=8)
    table.add_column("Duration", justify="right", min_width=10)

    for result in summary.results:
        status_str = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
        table.add_row(
            result.benchmark_id,
            result.tier,
            result.goal,
            status_str,
            f"{result.duration_seconds:.2f}s",
        )
        if not result.passed:
            for sig in result.signal_results:
                if not sig.passed:
                    table.add_row(
                        "",
                        "",
                        f"  [dim]↳ {sig.signal_type}: {sig.message}[/dim]",
                        "",
                        "",
                    )

    console.print(table)
    console.print(
        f"\n[bold]Total:[/bold] {summary.total}  "
        f"[green]{summary.passed} passed[/green]  "
        f"[red]{summary.failed} failed[/red]"
    )

    if save and summary.total > 0:
        sdd_dir = Path(".sdd")
        out = save_results(summary, sdd_dir)
        console.print(f"[dim]Results saved → {out}[/dim]")

    if summary.failed > 0:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------

cli.add_command(cost_cmd, "cost")


# ---------------------------------------------------------------------------
# agents — catalog management
# ---------------------------------------------------------------------------


@cli.group("agents")
def agents_group() -> None:
    """Manage agent catalogs: sync, list, and validate.

    \b
      bernstein agents sync               # refresh all catalogs
      bernstein agents list               # show all available agents
      bernstein agents list --source local  # filter by source
      bernstein agents validate           # check catalog health
    """


@agents_group.command("sync")
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Agent definitions directory.",
)
def agents_sync(definitions_dir: str) -> None:
    """Force-refresh all agent catalogs and update cache."""
    from bernstein.agents.registry import AgentRegistry

    definitions_path = Path(definitions_dir)

    # Provider: local YAML definitions
    console.print("[bold]Syncing agent catalogs…[/bold]\n")
    console.print(f"[cyan]→ local[/cyan]  {definitions_path}")

    if not definitions_path.exists():
        console.print(f"  [yellow]Directory does not exist:[/yellow] {definitions_path}")
        console.print(f"  [dim]Create it with: mkdir -p {definitions_path}[/dim]")
    else:
        registry = AgentRegistry(definitions_dir=definitions_path)
        loaded = registry.load_definitions()
        console.print(f"  [green]✓[/green] Loaded {len(loaded)} agent definition(s)")
        for defn in loaded:
            console.print(f"    [dim]{defn.name}[/dim] v{defn.version} ({defn.role})")

    # Provider: agency catalog (if present)
    agency_dir = Path(".sdd/agents/agency")
    console.print(f"\n[cyan]→ agency[/cyan] {agency_dir}")
    if not agency_dir.exists():
        console.print(f"  [dim]Directory not found — skipping (place Agency YAML files in {agency_dir})[/dim]")
    else:
        from bernstein.core.agency_loader import load_agency_catalog
        catalog = load_agency_catalog(agency_dir)
        console.print(f"  [green]✓[/green] Loaded {len(catalog)} agency agent(s)")
        for name in list(catalog)[:5]:
            agent = catalog[name]
            console.print(f"    [dim]{name}[/dim] ({agent.role})")
        if len(catalog) > 5:
            console.print(f"    [dim]… and {len(catalog) - 5} more[/dim]")

    console.print("\n[green]Sync complete.[/green]")


@agents_group.command("list")
@click.option(
    "--source",
    type=click.Choice(["local", "agency", "all"]),
    default="all",
    show_default=True,
    help="Filter agents by catalog source.",
)
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Local agent definitions directory.",
)
def agents_list(source: str, definitions_dir: str) -> None:
    """List all available agents from loaded catalogs."""
    from bernstein.agents.registry import AgentRegistry

    rows: list[tuple[str, str, str, str]] = []

    # Local definitions
    if source in ("local", "all"):
        definitions_path = Path(definitions_dir)
        if definitions_path.exists():
            registry = AgentRegistry(definitions_dir=definitions_path)
            registry.load_definitions()
            for defn in registry.definitions.values():
                rows.append((defn.name, defn.name, defn.role, "local"))

    # Agency catalog
    if source in ("agency", "all"):
        agency_dir = Path(".sdd/agents/agency")
        if agency_dir.exists():
            from bernstein.core.agency_loader import load_agency_catalog
            catalog = load_agency_catalog(agency_dir)
            for name, agent in catalog.items():
                rows.append((name, agent.name, agent.role, "agency"))

    if not rows:
        console.print("[dim]No agents found. Run [bold]bernstein agents sync[/bold] first.[/dim]")
        return

    from rich.table import Table

    table = Table(
        title="Available Agents",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("ID", style="dim", min_width=20)
    table.add_column("Name", min_width=20)
    table.add_column("Role", min_width=14)
    table.add_column("Source", min_width=8)

    for agent_id, name, role, src in sorted(rows, key=lambda r: (r[3], r[0])):
        src_color = "cyan" if src == "local" else "magenta"
        table.add_row(agent_id, name, role, f"[{src_color}]{src}[/{src_color}]")

    console.print(table)
    console.print(f"\n[dim]{len(rows)} agent(s) total[/dim]")


@agents_group.command("validate")
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Local agent definitions directory.",
)
def agents_validate(definitions_dir: str) -> None:
    """Validate all agent catalogs and report issues.

    Exits with code 1 if any provider is unreachable or has invalid agents.
    """
    import yaml

    from bernstein.agents.registry import SchemaValidationError

    definitions_path = Path(definitions_dir)
    issues: list[str] = []

    console.print("[bold]Validating agent catalogs…[/bold]\n")

    # --- Local definitions ---
    console.print(f"[cyan]→ local[/cyan]  {definitions_path}")
    if not definitions_path.exists():
        issues.append(f"local: definitions directory not found: {definitions_path}")
        console.print(f"  [red]✗[/red] Directory not found: {definitions_path}")
    else:
        yaml_files = list(definitions_path.glob("*.yaml")) + list(definitions_path.glob("*.yml"))
        if not yaml_files:
            console.print("  [dim]No YAML files found — catalog is empty[/dim]")
        for yaml_file in sorted(yaml_files):
            try:
                content = yaml_file.read_text(encoding="utf-8")
                data = yaml.safe_load(content)
                if not isinstance(data, dict):
                    raise ValueError("YAML must be a mapping")
                from bernstein.agents.registry import AgentRegistry
                registry = AgentRegistry(definitions_dir=definitions_path)
                registry._validate_schema(data, yaml_file)
                console.print(f"  [green]✓[/green] {yaml_file.name}")
            except SchemaValidationError as exc:
                issues.append(f"local/{yaml_file.name}: {exc}")
                console.print(f"  [red]✗[/red] {yaml_file.name}: {exc}")
            except Exception as exc:
                issues.append(f"local/{yaml_file.name}: {exc}")
                console.print(f"  [red]✗[/red] {yaml_file.name}: {exc}")

    # --- Agency catalog ---
    agency_dir = Path(".sdd/agents/agency")
    console.print(f"\n[cyan]→ agency[/cyan] {agency_dir}")
    if not agency_dir.exists():
        console.print("  [dim]Not configured — skipping[/dim]")
    else:
        from bernstein.core.agency_loader import parse_agency_agent
        agency_files = [
            p for p in sorted(agency_dir.iterdir())
            if p.suffix in (".yaml", ".yml")
        ]
        if not agency_files:
            console.print("  [dim]No YAML files found — catalog is empty[/dim]")
        for p in agency_files:
            try:
                parse_agency_agent(p)
                console.print(f"  [green]✓[/green] {p.name}")
            except ValueError as exc:
                issues.append(f"agency/{p.name}: {exc}")
                console.print(f"  [red]✗[/red] {p.name}: {exc}")

    # --- Summary ---
    console.print()
    if issues:
        console.print(f"[red]Validation failed: {len(issues)} issue(s)[/red]")
        for issue in issues:
            console.print(f"  [red]•[/red] {issue}")
        raise SystemExit(1)
    else:
        console.print("[green]All catalogs valid.[/green]")


# ---------------------------------------------------------------------------
# Backward-compatible aliases (old names still work)
# ---------------------------------------------------------------------------

# Hidden backward-compat aliases — old names still work
cli.add_command(click.Command("init", callback=init), "init")
cli.add_command(click.Command("run", callback=run, hidden=True), "run")
cli.add_command(click.Command("start", callback=start, hidden=True), "start")
cli.add_command(click.Command("status", callback=status, hidden=True), "status")
cli.add_command(click.Command("rest", callback=stop, hidden=True), "rest")
cli.add_command(click.Command("add-task", callback=add_task, hidden=True), "add-task")
cli.add_command(click.Command("logs-legacy", callback=_notes_legacy, hidden=True), "logs-legacy")
cli.add_command(click.Command("list-tasks", callback=list_tasks, hidden=True), "list-tasks")


# ---------------------------------------------------------------------------
# evolve  — manage evolution proposals
# ---------------------------------------------------------------------------


@cli.group("evolve")
def evolve() -> None:
    """Manage self-evolution proposals.

    \b
      bernstein evolve review           # list proposals pending human review
      bernstein evolve approve <id>     # approve a specific proposal
      bernstein evolve run              # run the autoresearch evolution loop
    """


@evolve.command("run")
@click.option(
    "--window",
    default="2h",
    show_default=True,
    help="Evolution window duration (e.g. 2h, 30m, 1h30m).",
)
@click.option(
    "--max-proposals",
    default=24,
    show_default=True,
    help="Maximum proposals to evaluate per session.",
)
@click.option(
    "--cycle",
    default=300,
    show_default=True,
    help="Seconds per experiment cycle (default 300 = 5 min).",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_run(window: str, max_proposals: int, cycle: int, workdir: str) -> None:
    """Run the autoresearch evolution loop.

    \b
    Runs time-boxed experiment cycles that:
    1. Analyze metrics and detect improvement opportunities
    2. Generate low-risk proposals (L0/L1 only)
    3. Sandbox validate each proposal
    4. Auto-apply improvements that pass validation
    5. Log all results to .sdd/evolution/experiments.jsonl

    L2+ proposals are saved to .sdd/evolution/deferred.jsonl for human review.

    \b
      bernstein evolve run                         # default: 2h window, 24 proposals
      bernstein evolve run --window 30m            # short session
      bernstein evolve run --max-proposals 48      # more experiments
    """
    from bernstein.evolution.loop import EvolutionLoop

    root = Path(workdir).resolve()
    state_dir = root / ".sdd"

    if not state_dir.is_dir():
        console.print(
            "[red].sdd directory not found.[/red] "
            "Run [bold]bernstein[/bold] first to initialise the workspace."
        )
        raise SystemExit(1)

    # Parse window duration string (e.g. "2h", "30m", "1h30m").
    window_seconds = _parse_duration(window)
    if window_seconds <= 0:
        console.print(f"[red]Invalid window duration:[/red] {window}")
        raise SystemExit(1)

    console.print(
        f"[bold]Evolution loop starting[/bold]\n"
        f"  Window:     {window} ({window_seconds}s)\n"
        f"  Max props:  {max_proposals}\n"
        f"  Cycle:      {cycle}s\n"
        f"  State dir:  {state_dir}\n"
    )

    loop = EvolutionLoop(
        state_dir=state_dir,
        repo_root=root,
        cycle_seconds=cycle,
        max_proposals=max_proposals,
        window_seconds=window_seconds,
    )

    try:
        results = loop.run(
            window_seconds=window_seconds,
            max_proposals=max_proposals,
        )
    except KeyboardInterrupt:
        loop.stop()
        results = loop._experiments
        console.print("\n[dim]Evolution loop interrupted.[/dim]")

    # Print summary.
    summary = loop.get_summary()
    console.print(
        f"\n[bold]Evolution complete[/bold]\n"
        f"  Experiments:  {summary['experiments_run']}\n"
        f"  Accepted:     {summary['proposals_accepted']}\n"
        f"  Rate:         {summary['acceptance_rate']:.0%}\n"
        f"  Cost:         ${summary['total_cost_usd']:.4f}\n"
        f"  Elapsed:      {summary['elapsed_seconds']:.0f}s\n"
    )

    if results:
        from rich.table import Table

        result_table = Table(
            title="Experiment Results",
            show_lines=False,
            header_style="bold cyan",
        )
        result_table.add_column("Proposal", min_width=12)
        result_table.add_column("Title", min_width=30)
        result_table.add_column("Risk", min_width=8)
        result_table.add_column("Delta", justify="right", min_width=8)
        result_table.add_column("Result", min_width=10)

        for r in results:
            color = "green" if r.accepted else "red"
            delta_str = f"{r.delta:+.3f}" if r.delta != 0 else "—"
            result_table.add_row(
                r.proposal_id,
                r.title,
                r.risk_level,
                delta_str,
                f"[{color}]{'accepted' if r.accepted else 'rejected'}[/{color}]",
            )
        console.print(result_table)


def _parse_duration(s: str) -> int:
    """Parse a duration string like '2h', '30m', '1h30m' into seconds."""
    import re as _re

    total = 0
    for match in _re.finditer(r"(\d+)\s*(h|m|s)", s.lower()):
        value = int(match.group(1))
        unit = match.group(2)
        if unit == "h":
            total += value * 3600
        elif unit == "m":
            total += value * 60
        elif unit == "s":
            total += value

    if total == 0:
        try:
            total = int(s)
        except ValueError:
            return 0
    return total


@evolve.command("review")
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_review(workdir: str) -> None:
    """Show upgrade proposals pending human review."""
    from bernstein.evolution.gate import ApprovalGate

    root = Path(workdir).resolve()
    decisions_dir = root / ".sdd" / "evolution"
    gate = ApprovalGate(decisions_dir=decisions_dir)
    pending = gate.get_pending_decisions()

    if not pending:
        console.print("[dim]No proposals pending review.[/dim]")
        return

    from rich.table import Table

    review_table = Table(title="Proposals Pending Review", show_lines=True, header_style="bold cyan")
    review_table.add_column("ID", style="dim", min_width=12)
    review_table.add_column("Risk", min_width=12)
    review_table.add_column("Confidence", justify="right", min_width=10)
    review_table.add_column("Outcome", min_width=22)
    review_table.add_column("Reason")

    for d in sorted(pending, key=lambda x: x.decided_at):
        outcome_color = "red" if "immediate" in d.outcome.value else "yellow"
        review_table.add_row(
            d.proposal_id,
            d.risk_level.value,
            f"{d.confidence:.0%}",
            f"[{outcome_color}]{d.outcome.value}[/{outcome_color}]",
            d.reason,
        )

    console.print(review_table)
    console.print(
        "\n[dim]Approve with:[/dim] [bold]bernstein evolve approve <id>[/bold]"
    )


@evolve.command("approve")
@click.argument("proposal_id")
@click.option(
    "--reviewer",
    default="human",
    show_default=True,
    help="Name of the approver.",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_approve(proposal_id: str, reviewer: str, workdir: str) -> None:
    """Approve an upgrade proposal by ID."""
    from bernstein.evolution.gate import ApprovalGate

    root = Path(workdir).resolve()
    decisions_dir = root / ".sdd" / "evolution"
    gate = ApprovalGate(decisions_dir=decisions_dir)
    decision = gate.approve(proposal_id, reviewer=reviewer)

    if decision is None:
        console.print(
            f"[red]No pending proposal found:[/red] {proposal_id}\n"
            "Run [bold]bernstein evolve review[/bold] to list pending proposals."
        )
        raise SystemExit(1)

    console.print(
        f"[green]Approved:[/green] [bold]{proposal_id}[/bold] "
        f"(reviewer={reviewer})"
    )
