"""CLI entry point for Bernstein -- multi-agent orchestration.

This module defines the top-level click group and registers all
subcommand modules.  Actual command implementations live in:

  helpers.py    — shared constants (SERVER_URL, console, STATUS_COLORS) and
                  utility functions (_server_get, _server_post, _read_pid, etc.)
  run_cmd.py    — init, conduct (run), downbeat (start), demo
  stop_cmd.py   — stop (soft/hard), shutdown signals, session save, ticket recovery
  status_cmd.py — status (score), ps, doctor
  agents_cmd.py — agents sync/list/validate/showcase/match/discover
  evolve_cmd.py — evolve run/review/approve/status/export
  cost.py       — cost breakdown command
  dashboard.py  — live Textual TUI / Rich Live display
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click
import httpx

# Subcommand imports
from bernstein.cli.agents_cmd import agents_group
from bernstein.cli.cost import cost_cmd
from bernstein.cli.evolve_cmd import evolve

# ---------------------------------------------------------------------------
# Re-export shared state so existing imports like
#   from bernstein.cli.main import console, SERVER_URL
# continue to work.
# ---------------------------------------------------------------------------

# Explicit __all__ so pyright knows these are intentional re-exports.
__all__ = [
    "BANNER",
    "DEMO_TASKS",
    "SDD_DIRS",
    "SDD_PID_SERVER",
    "SDD_PID_SPAWNER",
    "SDD_PID_WATCHDOG",
    "SERVER_URL",
    "STATUS_COLORS",
    "auth_headers",
    "console",
    "detect_available_adapter",
    "find_seed_file",
    "hard_stop",
    "is_alive",
    "is_process_alive",
    "kill_pid",
    "kill_pid_hard",
    "print_banner",
    "print_dry_run_table",
    "read_pid",
    "recover_orphaned_claims",
    "register_sigint_handler",
    "return_claimed_to_open",
    "save_session_on_stop",
    "server_get",
    "server_post",
    "setup_demo_project",
    "sigint_handler",
    "soft_stop",
    "write_pid",
    "write_shutdown_signals",
]
from bernstein.cli.helpers import (
    BANNER,
    SDD_DIRS,
    SDD_PID_SERVER,
    SDD_PID_SPAWNER,
    SDD_PID_WATCHDOG,
    SERVER_URL,
    STATUS_COLORS,
    auth_headers,
    console,
    find_seed_file,
    is_alive,
    is_process_alive,
    kill_pid,
    kill_pid_hard,
    print_banner,
    print_dry_run_table,
    read_pid,
    server_get,
    server_post,
    write_pid,
)

# Re-export run_cmd helpers used by tests
from bernstein.cli.run_cmd import (
    DEMO_TASKS,
    demo,
    detect_available_adapter,
    init,
    run,
    setup_demo_project,
    start,
)
from bernstein.cli.status_cmd import doctor as _doctor_impl
from bernstein.cli.status_cmd import ps_cmd, status

# Re-export stop_cmd helpers used by tests and other modules
from bernstein.cli.stop_cmd import (
    hard_stop,
    recover_orphaned_claims,
    register_sigint_handler,
    return_claimed_to_open,
    save_session_on_stop,
    sigint_handler,
    soft_stop,
    stop,
    write_shutdown_signals,
)

if TYPE_CHECKING:
    from rich.table import Table
    from rich.text import Text

    from bernstein.eval.golden import Tier

# ---------------------------------------------------------------------------
# Rich help
# ---------------------------------------------------------------------------


def _print_rich_help() -> None:
    """Print a grouped, color-coded help screen."""
    from rich.panel import Panel
    from rich.table import Table

    c = console
    c.print()
    c.print(
        Panel(
            "[bold]bernstein[/bold]  —  multi-agent orchestration for CLI coding agents",
            border_style="blue",
            padding=(0, 2),
        )
    )
    c.print("\n  [bold cyan]Quick start[/bold cyan]")
    c.print('  [dim]$[/dim] bernstein -g [green]"Add JWT auth with tests"[/green]     [dim]# inline goal[/dim]')
    c.print("  [dim]$[/dim] bernstein                                    [dim]# from bernstein.yaml[/dim]")
    c.print("  [dim]$[/dim] bernstein init                               [dim]# set up a new project[/dim]")
    c.print()

    groups: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "Run & manage",
            [
                ("bernstein -g [dim]GOAL[/dim]", "Orchestrate agents for an inline goal"),
                ("bernstein", "Run from bernstein.yaml or backlog"),
                ("init", "Initialize project (.sdd/ + bernstein.yaml)"),
                ("stop", "Graceful stop (agents save work first)"),
                ("stop --force", "Hard stop (kill immediately)"),
            ],
        ),
        (
            "Monitor",
            [
                ("status", "Task summary and agent health"),
                ("live", "Real-time TUI dashboard"),
                ("ps", "Running agent processes"),
                ("cost", "Spend breakdown by model and task"),
                ("logs", "Tail agent output"),
            ],
        ),
        (
            "Diagnostics",
            [
                ("doctor", "Pre-flight check: Python, adapters, API keys, ports"),
                ("recap", "Post-run summary: tasks, pass/fail, cost"),
                ("retro", "Detailed retrospective report"),
                ("plan", "Show task backlog"),
            ],
        ),
        (
            "Agents & evolution",
            [
                ("agents list", "Available agents and capabilities"),
                ("agents sync", "Pull latest agent catalog"),
                ("evolve", "Self-improvement proposals"),
                ("demo", "Zero-to-running demo in 60 seconds"),
            ],
        ),
    ]
    for group_name, commands in groups:
        table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
        table.add_column("Command", style="bold green", no_wrap=True, min_width=24)
        table.add_column("Description", style="dim")
        for cmd, desc in commands:
            table.add_row(cmd, desc)
        c.print(f"  [bold]{group_name}[/bold]")
        c.print(table)
        c.print()

    c.print("  [bold]Options[/bold]")
    opts = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    opts.add_column("Flag", style="yellow", no_wrap=True, min_width=24)
    opts.add_column("", style="dim")
    opts.add_row("--budget [dim]N[/dim]", "Cost cap in USD (0 = unlimited)")
    opts.add_row("--dry-run", "Preview task plan without spawning")
    opts.add_row("--approval [dim]auto|review|pr[/dim]", "Gate before merge")
    opts.add_row("--fresh", "Ignore saved session, start clean")
    opts.add_row("--version", "Show version")
    c.print(opts)
    c.print("\n  [dim]Docs:[/dim] https://chernistry.github.io/bernstein/")
    c.print("  [dim]Repo:[/dim] https://github.com/chernistry/bernstein\n")


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


class _RichGroup(click.Group):
    """Click group that renders help with Rich instead of plain text."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        _print_rich_help()

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Intercept ``--help-all`` so it works as both option and subcommand."""
        if "--help-all" in args:
            args = ["help-all"]
        return super().parse_args(ctx, args)


@click.group(cls=_RichGroup, invoke_without_command=True)
@click.version_option(package_name="bernstein")
@click.option("--goal", "-g", default=None, help="Inline goal (no seed file needed).")
@click.option("--evolve", "-e", is_flag=True, default=False, hidden=True, help="Continuous self-improvement mode.")
@click.option("--max-cycles", default=0, hidden=True, help="Stop after N evolve cycles (0=unlimited).")
@click.option("--budget", default=0.0, help="Cost cap in USD; stop spawning agents when reached (0=unlimited).")
@click.option("--interval", default=300, hidden=True, help="Seconds between evolve cycles (default 5min).")
@click.option(
    "--github", "github_sync", is_flag=True, default=False, hidden=True, help="Sync evolve proposals as GitHub Issues."
)
@click.option("--headless", is_flag=True, default=False, hidden=True, help="Run without dashboard (for overnight/CI).")
@click.option("--dry-run", is_flag=True, default=False, help="Preview task plan without spawning agents.")
@click.option("--yes", "-y", is_flag=True, default=False, hidden=True, help="Skip cost confirmation prompt.")
@click.option("--fresh", "force_fresh", is_flag=True, default=False, help="Ignore saved session; start from scratch.")
@click.option(
    "--approval",
    type=click.Choice(["auto", "review", "pr"]),
    default="auto",
    show_default=True,
    help="Approval gate: auto=merge immediately, review=pause for human review, pr=open GitHub PR.",
)
@click.option(
    "--merge",
    "merge_strategy",
    type=click.Choice(["pr", "direct"]),
    default="pr",
    show_default=True,
    help="Merge strategy: pr=create GitHub PR (default), direct=push directly to main branch.",
)
@click.option(
    "--cli",
    "cli_override",
    type=click.Choice(["claude", "codex", "gemini", "qwen", "auto"]),
    default=None,
    help="Force a specific agent (overrides auto-detection).",
)
@click.option(
    "--model",
    "model_override",
    default=None,
    metavar="MODEL",
    help="Force a specific model (e.g. opus, sonnet, o3).",
)
@click.pass_context
def cli(
    ctx: click.Context,
    goal: str | None,
    evolve: bool,
    max_cycles: int,
    budget: float,
    interval: int,
    github_sync: bool,
    headless: bool,
    dry_run: bool,
    yes: bool,
    force_fresh: bool,
    approval: str,
    merge_strategy: str,
    cli_override: str | None,
    model_override: str | None,
) -> None:
    """Multi-agent orchestration for CLI coding agents."""
    if ctx.invoked_subcommand is not None:
        return

    print_banner()

    seed_path = find_seed_file()
    workdir = Path.cwd()
    port = 8052

    if dry_run:
        print_dry_run_table(workdir)
        return

    # Recover orphaned claimed tickets from any prior crashed/stopped session
    recovered = recover_orphaned_claims()
    if recovered:
        console.print(f"[yellow]Recovered {recovered} orphaned ticket(s) from a prior session.[/yellow]")

    # Evolve mode safety: require --budget or --max-cycles
    if evolve and budget <= 0 and max_cycles <= 0:
        from bernstein.cli.errors import BernsteinError

        BernsteinError(
            what="Evolve mode requires a safety limit",
            why="Evolve mode will autonomously modify code indefinitely",
            fix="Add --budget 5.00 or --max-cycles 10",
        ).print()
        raise SystemExit(1)

    # Evolve mode confirmation: show budget/cycles and require explicit approval
    if evolve and not yes:
        budget_str = f"${budget:.2f}" if budget > 0 else "unlimited"
        cycles_str = str(max_cycles) if max_cycles > 0 else "unlimited"
        console.print(
            f"[bold yellow]Evolve mode[/bold yellow] will autonomously modify code.\n"
            f"  Budget: [bold]{budget_str}[/bold], max cycles: [bold]{cycles_str}[/bold].\n"
            f"  Press [bold]Enter[/bold] to continue or Ctrl+C to cancel."
        )
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Aborted.[/yellow]")

    # Check if already running
    server_pid_path = Path(SDD_PID_SERVER)
    server_pid = read_pid(str(server_pid_path))
    already_running = server_pid is not None and is_alive(server_pid)

    if not already_running:
        # Write run_config.json so the orchestrator subprocess can read budget_usd, approval,
        # merge_strategy, and other per-run settings.
        if budget > 0 or approval != "auto" or merge_strategy != "pr":
            import json as _json

            runtime_dir = workdir / ".sdd" / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            run_cfg: dict[str, Any] = {}
            if budget > 0:
                run_cfg["budget_usd"] = budget
            if approval != "auto":
                run_cfg["approval"] = approval
            run_cfg["merge_strategy"] = merge_strategy
            (runtime_dir / "run_config.json").write_text(_json.dumps(run_cfg))

        if goal is not None:
            # Inline goal — no config files needed
            if not yes:
                console.print(
                    "[bold yellow]Cost estimate:[/bold yellow] ~$0.10-$1.00 per task with Sonnet. "
                    "Press [bold]Enter[/bold] to continue or Ctrl+C to cancel."
                )
                try:
                    input()
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]Aborted.[/yellow]")
            from bernstein.core.bootstrap import bootstrap_from_goal

            try:
                bootstrap_from_goal(
                    goal,
                    workdir=workdir,
                    port=port,
                    force_fresh=force_fresh,
                    cli=cli_override or "auto",
                    model=model_override,
                )
            except RuntimeError as exc:
                console.print(f"[red]Error:[/red] {exc}")
                raise SystemExit(1) from exc
        elif seed_path is not None:
            console.print(f"Using: [bold]{seed_path.name}[/bold]")
            from bernstein.core.bootstrap import bootstrap_from_seed
            from bernstein.core.seed import SeedError

            try:
                bootstrap_from_seed(seed_path, workdir=workdir, port=port, force_fresh=force_fresh)
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
                    bootstrap_from_goal("Execute backlog tasks", workdir=workdir, port=port, force_fresh=force_fresh)
                except RuntimeError as exc:
                    console.print(f"[red]Error:[/red] {exc}")
                    raise SystemExit(1) from exc
            else:
                console.print(
                    "No bernstein.yaml or backlog tasks found.\n\n"
                    "[bold]Quick start:[/bold]\n"
                    '  bernstein -g "Build a REST API with auth"\n\n'
                    "Or create a bernstein.yaml / add .md tasks to .sdd/backlog/open/\n"
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
            "github_sync": github_sync,
        }
        evolve_path = workdir / ".sdd" / "runtime" / "evolve.json"
        evolve_path.parent.mkdir(parents=True, exist_ok=True)
        evolve_path.write_text(_json.dumps(evolve_config))
        console.print(
            f"[bold cyan]Evolve mode ON[/bold cyan] "
            f"(interval={interval}s"
            f"{f', max_cycles={max_cycles}' if max_cycles else ''}"
            f"{f', budget=${budget:.2f}' if budget else ''}"
            f"{', github-sync=on' if github_sync else ''})"
        )

    if headless:
        console.print("[bold green]Running headless.[/bold green] Check .sdd/runtime/ for logs.")
        return

    # Register Ctrl+C handler so we save state before the dashboard exits
    register_sigint_handler()

    # Show live dashboard (blocks until Ctrl+C / q)
    from bernstein.cli.dashboard import run_dashboard

    run_dashboard()


# ---------------------------------------------------------------------------
# Register subcommands from modules
# ---------------------------------------------------------------------------

# Primary names
cli.add_command(stop, "stop")
cli.add_command(ps_cmd, "ps")
# doctor is registered via @cli.command decorator below
cli.add_command(agents_group, "agents")
cli.add_command(evolve, "evolve")
cli.add_command(demo, "demo")
cli.add_command(cost_cmd, "cost")

# Hidden "music" names (original names)
cli.add_command(init, "overture")
cli.add_command(run, "conduct")
cli.add_command(start, "downbeat")
cli.add_command(status, "score")

# Backward-compatible aliases
cli.add_command(init, "init")
cli.add_command(run, "run")
cli.add_command(start, "start")
cli.add_command(status, "status")
# Musical alias: "rest" = stop (hidden to avoid clutter)
_rest = click.Command("rest", callback=stop.callback, params=stop.params, hidden=True, help=stop.help)
cli.add_command(_rest)


# ---------------------------------------------------------------------------
# Remaining commands that are small enough to stay inline
# ---------------------------------------------------------------------------


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

    result = server_post("/task", payload)
    if result is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    task_id = result.get("id", "?")
    console.print(
        f"[green]Task added:[/green] [bold]{task_id}[/bold] — {title} ([dim]role={role}, priority={priority}[/dim])"
    )


cli.add_command(add_task, "add-task")


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
        console.print(f"[green]Created {len(result.created)} task(s):[/green] " + ", ".join(result.created))
    if result.skipped:
        console.print(f"[dim]Skipped {len(result.skipped)} file(s) already on server[/dim]")
    if result.moved:
        console.print(
            f"[green]Moved {len(result.moved)} completed file(s) to backlog/done/:[/green] " + ", ".join(result.moved)
        )
    for err in result.errors:
        console.print(f"[red]Error:[/red] {err}")

    if not result.created and not result.moved and not result.errors:
        if result.skipped:
            console.print("[dim]All backlog files already synced.[/dim]")
        else:
            console.print("[dim]Nothing to sync — backlog/open/ is empty.[/dim]")


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.option("--reason", "-r", default="Cancelled by user", help="Cancellation reason")
def cancel(task_id: str, reason: str) -> None:
    """Cancel a running or queued task."""
    data = server_post(f"/tasks/{task_id}/cancel", {"reason": reason})
    if data is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)
    console.print(f"[green]Cancelled:[/green] {data['title']}")
    console.print(f"[dim]Status: {data['status']}[/dim]")


# ---------------------------------------------------------------------------
# approve / reject / pending
# ---------------------------------------------------------------------------


@cli.command("approve")
@click.argument("task_id")
@click.option("--workdir", default=".", help="Project root directory.", type=click.Path())
def approve(task_id: str, workdir: str) -> None:
    """Approve a pending task review so Bernstein merges the work.

    When running with ``--approval review``, Bernstein pauses after each
    verified task and writes a pending approval file.  Run this command
    to signal approval so the orchestrator continues with the merge.

    \b
    Example:
      bernstein approve T-abc123
    """
    approvals_dir = Path(workdir) / ".sdd" / "runtime" / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    decision_file = approvals_dir / f"{task_id}.approved"
    decision_file.write_text("approved")
    console.print(f"[green]Approved:[/green] task [bold]{task_id}[/bold] — Bernstein will merge the work.")


@cli.command("reject")
@click.argument("task_id")
@click.option("--workdir", default=".", help="Project root directory.", type=click.Path())
def reject(task_id: str, workdir: str) -> None:
    """Reject a pending task review so Bernstein discards the work.

    When running with ``--approval review``, Bernstein pauses after each
    verified task and writes a pending approval file.  Run this command
    to reject the work -- the worktree will be cleaned up without merging.

    \b
    Example:
      bernstein reject T-abc123
    """
    approvals_dir = Path(workdir) / ".sdd" / "runtime" / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    decision_file = approvals_dir / f"{task_id}.rejected"
    decision_file.write_text("rejected")
    console.print(f"[red]Rejected:[/red] task [bold]{task_id}[/bold] — work will be discarded.")


@cli.command("pending")
@click.option("--workdir", default=".", help="Project root directory.", type=click.Path())
def pending(workdir: str) -> None:
    """List tasks waiting for approval review.

    Shows all tasks that have been verified and are waiting for a human
    decision (``bernstein approve <id>`` or ``bernstein reject <id>``).
    """
    from rich.table import Table

    pending_dir = Path(workdir) / ".sdd" / "runtime" / "pending_approvals"
    if not pending_dir.exists() or not any(pending_dir.glob("*.json")):
        console.print("[dim]No tasks pending approval.[/dim]")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Task ID", style="cyan")
    table.add_column("Title")
    table.add_column("Tests")

    for f in sorted(pending_dir.glob("*.json")):
        try:
            import json as _json

            data = _json.loads(f.read_text())
            table.add_row(
                data.get("task_id", f.stem),
                data.get("task_title", ""),
                data.get("test_summary", ""),
            )
        except Exception:
            table.add_row(f.stem, "[dim]unreadable[/dim]", "")

    console.print(table)
    console.print("\n[dim]Approve with:[/dim] bernstein approve <task_id>")
    console.print("[dim]Reject with:[/dim]  bernstein reject <task_id>")


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

    raw = server_get(path)
    if raw is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    tasks: list[dict[str, Any]] = cast("list[dict[str, Any]]", raw) if isinstance(raw, list) else []

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
        raw_status: str = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        depends = ", ".join(d[:8] for d in cast("list[str]", t.get("depends_on", []))) or "—"
        table.add_row(
            str(t.get("id", "—"))[:8],
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("role", "—")),
            str(t.get("title", "—")),
            depends,
            str(t.get("model") or "—"),
            str(t.get("effort") or "—"),
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
@click.option(
    "--runtime-dir",
    default=".sdd/runtime",
    show_default=True,
    hidden=True,
    help="Directory containing agent log files.",
)
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


cli.add_command(_notes_legacy, "logs-legacy")


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
    data = server_get("/status")
    if data is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
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


cli.add_command(list_tasks, "list-tasks")


# ---------------------------------------------------------------------------
# live — legacy display helpers (kept for test backward compatibility,
# superseded by bernstein.cli.live.LiveView)
# ---------------------------------------------------------------------------


def _build_agents_table(agents: list[dict[str, Any]]) -> Table:  # pyright: ignore[reportUnusedFunction]
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


def _build_events_table(tasks: list[dict[str, Any]]) -> Table:  # pyright: ignore[reportUnusedFunction]
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


def _build_stats_bar(summary: dict[str, Any]) -> Text:  # pyright: ignore[reportUnusedFunction]
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
@click.option(
    "--classic",
    is_flag=True,
    default=False,
    help="Use the classic Rich Live display instead of the Textual TUI.",
)
def live(interval: float, classic: bool) -> None:
    """Live dashboard: active agents, task events, and stats (Ctrl+C to exit).

    Launches the Textual TUI session manager by default.
    Pass --classic for the original Rich Live display.
    """
    if not classic:
        from bernstein.tui.app import BernsteinApp

        app = BernsteinApp(poll_interval=interval)
        app.run()
        return

    # -- classic Rich Live display using the LiveView module --
    from bernstein.cli.live import LiveView

    print_banner()

    view = LiveView(
        server_url=SERVER_URL,
        interval=interval,
    )
    view.run()


# ---------------------------------------------------------------------------
# web dashboard
# ---------------------------------------------------------------------------


@cli.command("dashboard")
@click.option("--port", default=8052, show_default=True, help="Server port.")
@click.option("--no-open", is_flag=True, default=False, help="Do not open browser.")
def dashboard(port: int, no_open: bool) -> None:
    """Open the web dashboard in your browser.

    Requires the Bernstein server to be running. If it is not,
    prints instructions on how to start it.
    """
    import webbrowser

    url = f"http://localhost:{port}/dashboard"
    # Check if server is alive
    try:
        resp = httpx.get(f"http://localhost:{port}/health", timeout=2.0)
        if resp.status_code != 200:
            console.print(
                f"[red]Server returned {resp.status_code}.[/red]\nStart the server first: [cyan]bernstein run[/cyan]"
            )
            sys.exit(1)
    except httpx.ConnectError:
        console.print(
            "[red]Cannot connect to Bernstein server.[/red]\n"
            f"Start the server first: [cyan]bernstein run[/cyan]\n"
            f"Then open: [link={url}]{url}[/link]"
        )
        sys.exit(1)

    console.print(f"[green]Dashboard:[/green] [link={url}]{url}[/link]")
    if not no_open:
        webbrowser.open(url)


# ---------------------------------------------------------------------------
# benchmark
# ---------------------------------------------------------------------------


@cli.group("benchmark")
def benchmark_group() -> None:
    """Run the tiered golden benchmark suite."""


@benchmark_group.command("swe-bench")
@click.option("--lite", "mode", flag_value="lite", default=True, help="Run SWE-Bench Lite (300 instances).")
@click.option("--sample", "sample", type=int, default=None, help="Evaluate a random sample of N instances.")
@click.option("--instance", "instance_id", default=None, help="Evaluate a single instance by ID.")
@click.option("--dataset", "dataset_path", default=None, help="Path to local JSONL dataset file.")
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Persist results to .sdd/benchmark/swe_bench_results.json.",
)
def benchmark_swe_bench(
    mode: str,
    sample: int | None,
    instance_id: str | None,
    dataset_path: str | None,
    save: bool,
) -> None:
    """Run Bernstein against SWE-Bench instances and report resolve rate.

    \b
      bernstein benchmark swe-bench --lite              # all 300 Lite instances
      bernstein benchmark swe-bench --sample 20         # random 20-instance eval
      bernstein benchmark swe-bench --instance django__django-11905
    """
    from rich.table import Table

    from bernstein.benchmark.swe_bench import InstanceResult, SWEBenchRunner, compute_report, save_results

    workdir = Path(".")
    runner = SWEBenchRunner(workdir=workdir, sample=sample, instance_id=instance_id)

    dpath = Path(dataset_path) if dataset_path else None
    instances = runner.load_dataset(dpath)

    if not instances:
        console.print(
            "[yellow]No instances found. Pass --dataset <path.jsonl> or install the 'datasets' package.[/yellow]"
        )
        raise SystemExit(1)

    console.print(f"[bold]SWE-Bench evaluation[/bold] — {len(instances)} instance(s)")

    table = Table(title="SWE-Bench Results", header_style="bold cyan", show_lines=False)
    table.add_column("Instance", style="dim", min_width=30)
    table.add_column("Resolved", min_width=10)
    table.add_column("Cost (USD)", justify="right", min_width=12)
    table.add_column("Time (s)", justify="right", min_width=10)
    table.add_column("Agents", justify="right", min_width=8)

    results: list[InstanceResult] = []
    for inst in instances:
        console.print(f"  Running [cyan]{inst.instance_id}[/cyan]…", end="")
        result = runner.run_instance(inst)
        results.append(result)
        status_icon = "[green]✓[/green]" if result.resolved else "[red]✗[/red]"
        console.print(f" {status_icon}")
        table.add_row(
            inst.instance_id,
            "[green]YES[/green]" if result.resolved else "[red]NO[/red]",
            f"${result.cost_usd:.4f}",
            f"{result.duration_seconds:.1f}",
            str(result.agent_count),
        )

    report = compute_report(results)
    console.print(table)
    console.print(
        f"\n[bold]Resolve rate:[/bold] {report.resolve_rate:.1%} "
        f"({report.resolved}/{report.total})  "
        f"[dim]median cost ${report.median_cost_usd:.4f}  "
        f"median time {report.median_duration_seconds:.0f}s[/dim]"
    )

    if save:
        sdd_dir = Path(".sdd")
        out = save_results(report, sdd_dir)
        console.print(f"[dim]Results saved → {out}[/dim]")


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
# eval — multiplicative scoring harness
# ---------------------------------------------------------------------------


@cli.group("eval")
def eval_group() -> None:
    """Evaluation harness with multiplicative scoring."""


@eval_group.command("run")
@click.option(
    "--tier",
    type=click.Choice(["smoke", "standard", "stretch", "adversarial"]),
    default=None,
    help="Run only tasks from this tier.",
)
@click.option("--compare", "compare_prev", is_flag=True, default=False, help="Compare vs previous run.")
@click.option("--save/--no-save", default=True, show_default=True, help="Persist results to disk.")
def eval_run(tier: str | None, compare_prev: bool, save: bool) -> None:
    """Run the golden benchmark suite with multiplicative scoring.

    \b
      bernstein eval run                    # run full golden suite
      bernstein eval run --tier smoke       # smoke tier only
      bernstein eval run --compare          # compare vs previous run
    """
    from rich.table import Table

    from bernstein.eval.harness import EvalHarness, TaskEvalResult

    workdir = Path(".")
    state_dir = workdir / ".sdd"
    harness = EvalHarness(state_dir=state_dir, repo_root=workdir)

    tier_filter: Tier | None = tier  # type: ignore[assignment]
    tasks = harness.load_golden_tasks(tier_filter=tier_filter)

    if not tasks:
        console.print("[yellow]No golden tasks found.[/yellow]")
        console.print(f"[dim]Expected at: {state_dir / 'eval' / 'golden'}/<tier>/*.md[/dim]")
        raise SystemExit(1)

    console.print(f"[bold]Eval harness[/bold] — {len(tasks)} golden task(s)")

    # Evaluate each task (with empty telemetry for now — real runs
    # would collect telemetry from actual agent execution)
    task_results: list[TaskEvalResult] = []
    for task in tasks:
        result = harness.evaluate_task(task)
        task_results.append(result)

    run_result = harness.compute_multiplicative_score(task_results)

    # Display results
    table = Table(title="Eval Results", header_style="bold cyan", show_lines=False)
    table.add_column("Component", min_width=15)
    table.add_column("Score", justify="right", min_width=10)

    mc = run_result.multiplicative_components
    if mc:
        table.add_row("Task Success", f"{mc.task_success:.2%}")
        table.add_row("Code Quality", f"{mc.code_quality:.2%}")
        table.add_row("Efficiency", f"{mc.efficiency:.2%}")
        table.add_row("Reliability", f"{mc.reliability:.2%}")
        table.add_row("Safety", f"{mc.safety:.2%}")
        table.add_row("", "")
        table.add_row("[bold]Final Score[/bold]", f"[bold]{mc.final_score:.4f}[/bold]")

    console.print(table)

    # Per-tier breakdown
    pt = run_result.per_tier
    if pt:
        tier_table = Table(title="Per-Tier Scores", header_style="bold cyan")
        tier_table.add_column("Tier", min_width=15)
        tier_table.add_column("Score", justify="right", min_width=10)
        tier_table.add_row("Smoke", f"{pt.smoke:.2%}")
        tier_table.add_row("Standard", f"{pt.standard:.2%}")
        tier_table.add_row("Stretch", f"{pt.stretch:.2%}")
        tier_table.add_row("Adversarial", f"{pt.adversarial:.2%}")
        console.print(tier_table)

    # Compare with previous run
    if compare_prev:
        prev = harness.load_previous_run()
        if prev:
            delta = run_result.score - prev.score
            color = "green" if delta >= 0 else "red"
            console.print(f"\n[bold]vs previous:[/bold] [{color}]{delta:+.4f}[/{color}]")
            console.print(f"[dim]Previous score: {prev.score:.4f}[/dim]")
        else:
            console.print("[dim]No previous run found for comparison.[/dim]")

    # Save results
    if save:
        path = harness.save_run(run_result)
        console.print(f"[dim]Results saved → {path}[/dim]")


@eval_group.command("report")
def eval_report() -> None:
    """Generate a markdown report from the most recent eval run."""
    from bernstein.eval.harness import EvalHarness

    workdir = Path(".")
    state_dir = workdir / ".sdd"
    harness = EvalHarness(state_dir=state_dir, repo_root=workdir)

    prev = harness.load_previous_run()
    if not prev:
        console.print("[yellow]No eval runs found.[/yellow]")
        raise SystemExit(1)

    console.print(f"[bold]Eval Report[/bold] — score: {prev.score:.4f}")

    mc = prev.multiplicative_components
    if mc:
        console.print(f"  Task Success:  {mc.task_success:.2%}")
        console.print(f"  Code Quality:  {mc.code_quality:.2%}")
        console.print(f"  Efficiency:    {mc.efficiency:.2%}")
        console.print(f"  Reliability:   {mc.reliability:.2%}")
        console.print(f"  Safety:        {mc.safety:.2%}")

    pt = prev.per_tier
    if pt:
        console.print(f"\n  Smoke:       {pt.smoke:.2%}")
        console.print(f"  Standard:    {pt.standard:.2%}")
        console.print(f"  Stretch:     {pt.stretch:.2%}")
        console.print(f"  Adversarial: {pt.adversarial:.2%}")

    if prev.cost_total > 0:
        console.print(f"\n  Total cost: ${prev.cost_total:.2f}")

    console.print(f"  Tasks evaluated: {prev.tasks_evaluated}")


@eval_group.command("failures")
def eval_failures() -> None:
    """Show failure taxonomy breakdown from the most recent eval run."""
    import json as json_mod

    from rich.table import Table

    workdir = Path(".")
    runs_dir = workdir / ".sdd" / "eval" / "runs"

    if not runs_dir.is_dir():
        console.print("[yellow]No eval runs found.[/yellow]")
        raise SystemExit(1)

    run_files = sorted(runs_dir.glob("eval_run_*.json"), reverse=True)
    if not run_files:
        console.print("[yellow]No eval runs found.[/yellow]")
        raise SystemExit(1)

    data = json_mod.loads(run_files[0].read_text(encoding="utf-8"))
    failures = data.get("failures", [])

    if not failures:
        console.print("[green]No failures in the most recent run.[/green]")
        return

    table = Table(title="Failure Taxonomy", header_style="bold red", show_lines=True)
    table.add_column("Task", min_width=20)
    table.add_column("Category", min_width=18)
    table.add_column("Details", min_width=40)

    for f in failures:
        table.add_row(
            str(f.get("task", "")),
            str(f.get("taxonomy", "")),
            str(f.get("details", "")),
        )

    console.print(table)

    # Category counts
    counts: dict[str, int] = {}
    for f in failures:
        cat = str(f.get("taxonomy", "unknown"))
        counts[cat] = counts.get(cat, 0) + 1

    console.print(f"\n[bold]Total failures:[/bold] {len(failures)}")
    for cat, count in sorted(counts.items(), key=lambda x: -x[1]):
        console.print(f"  {cat}: {count}")


# ---------------------------------------------------------------------------
# workspace — multi-repo workspace management
# ---------------------------------------------------------------------------


@cli.group("workspace", invoke_without_command=True)
@click.pass_context
def workspace_group(ctx: click.Context) -> None:
    """Multi-repo workspace management.

    Without a subcommand, shows repo status table.
    """
    if ctx.invoked_subcommand is not None:
        return

    from rich.table import Table

    data = server_get("/workspace")
    if data is None:
        # No server running — try to parse workspace from seed file
        seed_path = find_seed_file()
        if seed_path is None:
            console.print("[dim]No workspace configured (no bernstein.yaml found).[/dim]")
            return

        from bernstein.core.seed import SeedError, parse_seed

        try:
            cfg = parse_seed(seed_path)
        except SeedError as exc:
            from bernstein.cli.errors import seed_parse_error

            seed_parse_error(exc).print()
            return

        if cfg.workspace is None:
            console.print("[dim]No workspace section in bernstein.yaml.[/dim]")
            return

        ws = cfg.workspace
        repo_statuses = ws.status()

        table = Table(title="Workspace repos", show_header=True, header_style="bold magenta")
        table.add_column("Repo", style="cyan")
        table.add_column("Path")
        table.add_column("Branch", justify="center")
        table.add_column("Clean", justify="center")
        table.add_column("Ahead", justify="right")
        table.add_column("Behind", justify="right")

        for repo in ws.repos:
            rs = repo_statuses.get(repo.name)
            if rs:
                clean_icon = "[green]yes[/green]" if rs.clean else "[red]no[/red]"
                table.add_row(repo.name, str(repo.path), rs.branch, clean_icon, str(rs.ahead), str(rs.behind))
            else:
                table.add_row(repo.name, str(repo.path), "[dim]n/a[/dim]", "[dim]n/a[/dim]", "-", "-")

        console.print(table)
        return

    # Server is running — render from API response
    from rich.table import Table as RichTable

    table = RichTable(title="Workspace repos", show_header=True, header_style="bold magenta")
    table.add_column("Repo", style="cyan")
    table.add_column("Path")
    table.add_column("Branch", justify="center")
    table.add_column("Clean", justify="center")
    table.add_column("Ahead", justify="right")
    table.add_column("Behind", justify="right")

    for repo in data.get("repos", []):
        clean_icon = "[green]yes[/green]" if repo.get("clean") else "[red]no[/red]"
        table.add_row(
            repo["name"],
            repo["path"],
            repo.get("branch", "unknown"),
            clean_icon,
            str(repo.get("ahead", 0)),
            str(repo.get("behind", 0)),
        )

    console.print(table)


@workspace_group.command("clone")
def workspace_clone() -> None:
    """Clone all missing repos defined in the workspace."""
    seed_path = find_seed_file()
    if seed_path is None:
        from bernstein.cli.errors import no_seed_file

        no_seed_file().print()
        return

    from bernstein.core.seed import SeedError, parse_seed

    try:
        cfg = parse_seed(seed_path)
    except SeedError as exc:
        from bernstein.cli.errors import seed_parse_error

        seed_parse_error(exc).print()
        return

    if cfg.workspace is None:
        console.print("[dim]No workspace section in bernstein.yaml.[/dim]")
        return

    cloned = cfg.workspace.clone_missing()
    if cloned:
        for name in cloned:
            console.print(f"[green]Cloned[/green] {name}")
    else:
        console.print("[dim]All repos already present (or no clone URLs configured).[/dim]")


@workspace_group.command("validate")
def workspace_validate() -> None:
    """Check workspace health -- all repos exist and are valid git repos."""
    seed_path = find_seed_file()
    if seed_path is None:
        from bernstein.cli.errors import no_seed_file

        no_seed_file().print()
        return

    from bernstein.core.seed import SeedError, parse_seed

    try:
        cfg = parse_seed(seed_path)
    except SeedError as exc:
        from bernstein.cli.errors import seed_parse_error

        seed_parse_error(exc).print()
        return

    if cfg.workspace is None:
        console.print("[dim]No workspace section in bernstein.yaml.[/dim]")
        return

    issues = cfg.workspace.validate()
    if issues:
        for issue in issues:
            console.print(f"[red]Issue:[/red] {issue}")
    else:
        console.print(f"[green]All {len(cfg.workspace.repos)} repos are healthy.[/green]")


# ---------------------------------------------------------------------------
# config — global ~/.bernstein config management
# ---------------------------------------------------------------------------


@cli.group("config")
def config_group() -> None:
    """Manage global Bernstein configuration (~/.bernstein/config.yaml)."""


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a global config value.

    Example: bernstein config set cli codex
    """
    from bernstein.core.home import BernsteinHome

    home = BernsteinHome.default()
    # Coerce numeric strings
    parsed_value: Any
    try:
        parsed_value = float(value) if "." in value else int(value)
    except ValueError:
        parsed_value = value if value.lower() not in ("null", "none") else None
    home.set(key, parsed_value)
    console.print(f"[green]✓[/green] {key} = {parsed_value!r}  [dim](~/.bernstein/config.yaml)[/dim]")


@config_group.command("get")
@click.argument("key")
@click.option("--project-dir", default=".", show_default=True, help="Project directory for precedence check.")
def config_get(key: str, project_dir: str) -> None:
    """Show the effective value for KEY and its source.

    Example: bernstein config get cli
    """
    from bernstein.core.home import BernsteinHome, resolve_config

    home = BernsteinHome.default()
    result = resolve_config(key, home=home, project_dir=Path(project_dir))
    source_style = {"project": "cyan", "global": "yellow", "default": "dim"}.get(result["source"], "white")
    console.print(
        f"[bold]{key}[/bold] = {result['value']!r}  [{source_style}](source: {result['source']})[/{source_style}]"
    )


@config_group.command("list")
@click.option("--project-dir", default=".", show_default=True, help="Project directory for precedence check.")
def config_list(project_dir: str) -> None:
    """List all config keys with their effective values and sources."""
    from rich.table import Table

    from bernstein.core.home import _DEFAULTS, BernsteinHome, resolve_config  # type: ignore[reportPrivateUsage]

    home = BernsteinHome.default()
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Key")
    table.add_column("Value")
    table.add_column("Source")

    source_styles = {"project": "cyan", "global": "yellow", "default": "dim"}

    for key in sorted(_DEFAULTS.keys()):
        result = resolve_config(key, home=home, project_dir=Path(project_dir))
        style = source_styles.get(result["source"], "white")
        table.add_row(
            key,
            str(result["value"]),
            f"[{style}]{result['source']}[/{style}]",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# retro — on-demand retrospective report
# ---------------------------------------------------------------------------


def _load_archive_tasks(
    archive_path: Path,
    since_ts: float | None,
) -> tuple[list[Any], list[Any]]:
    """Load done/failed Task objects from the archive JSONL file.

    Args:
        archive_path: Path to .sdd/archive/tasks.jsonl.
        since_ts: If set, only include tasks completed after this timestamp.

    Returns:
        Tuple of (done_tasks, failed_tasks) as Task objects.
    """
    from bernstein.core.models import Complexity, Scope, Task, TaskStatus

    done_tasks: list[Task] = []
    failed_tasks: list[Task] = []

    if not archive_path.exists():
        return done_tasks, failed_tasks

    for line in archive_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        status_str = record.get("status", "")
        if status_str not in ("done", "failed"):
            continue

        completed_at = record.get("completed_at")
        if since_ts is not None and (completed_at is None or completed_at < since_ts):
            continue

        task = Task(
            id=record.get("task_id", ""),
            title=record.get("title", ""),
            description=record.get("result_summary", "") or "",
            role=record.get("role", "unknown"),
            complexity=Complexity(record.get("complexity", "medium")),
            scope=Scope(record.get("scope", "medium")),
            status=TaskStatus.DONE if status_str == "done" else TaskStatus.FAILED,
            created_at=record.get("created_at", 0.0),
        )
        if status_str == "done":
            done_tasks.append(task)
        else:
            failed_tasks.append(task)

    return done_tasks, failed_tasks


def _build_collector_from_archive(
    archive_path: Path,
    since_ts: float | None,
) -> Any:
    """Build a MetricsCollector populated from the archive JSONL file.

    Args:
        archive_path: Path to .sdd/archive/tasks.jsonl.
        since_ts: If set, only include tasks completed after this timestamp.

    Returns:
        MetricsCollector with task metrics pre-populated.
    """
    from bernstein.core.metrics import MetricsCollector, TaskMetrics

    collector = MetricsCollector(metrics_dir=None)

    if not archive_path.exists():
        return collector

    for line in archive_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        status_str = record.get("status", "")
        if status_str not in ("done", "failed"):
            continue

        completed_at: float | None = record.get("completed_at")
        if since_ts is not None and (completed_at is None or completed_at < since_ts):
            continue

        task_id = record.get("task_id", "")
        role = record.get("role", "unknown")
        model = record.get("model") or "unknown"
        provider = record.get("provider") or "unknown"
        start_time: float = record.get("created_at") or 0.0
        cost_usd: float = record.get("cost_usd") or 0.0

        tm = TaskMetrics(
            task_id=task_id,
            role=role,
            model=model,
            provider=provider,
            start_time=start_time,
            end_time=completed_at,
            success=(status_str == "done"),
            cost_usd=cost_usd,
        )
        collector._task_metrics[task_id] = tm  # type: ignore[reportPrivateUsage]

    # Enrich with token data from .sdd/metrics/tasks.jsonl if available
    metrics_path = archive_path.parent.parent / "metrics" / "tasks.jsonl"
    if metrics_path.exists():
        metrics_by_task: dict[str, dict[str, Any]] = {}
        for line in metrics_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = rec.get("task_id", "")
            if tid:
                metrics_by_task[str(tid)] = rec

        for task_id, tm in collector._task_metrics.items():  # type: ignore[reportPrivateUsage]
            if task_id in metrics_by_task:
                rec = metrics_by_task[task_id]
                tm.tokens_prompt = int(rec.get("tokens_prompt", 0) or 0)
                tm.tokens_completion = int(rec.get("tokens_completion", 0) or 0)
                tm.tokens_used = tm.tokens_prompt + tm.tokens_completion
                # Use metrics cost if archive cost is missing
                if not tm.cost_usd and rec.get("cost_usd"):
                    tm.cost_usd = float(rec.get("cost_usd") or 0.0)

    return collector


@cli.command("retro")
@click.option(
    "--since",
    default=None,
    metavar="HOURS",
    type=float,
    help="Only include tasks completed in the last N hours.",
)
@click.option(
    "--output",
    "-o",
    default=None,
    metavar="FILE",
    help="Write report to FILE instead of stdout (default: .sdd/runtime/retrospective.md).",
)
@click.option(
    "--print",
    "print_output",
    is_flag=True,
    default=False,
    help="Print report to stdout even when writing to file.",
)
@click.option(
    "--archive",
    default=".sdd/archive/tasks.jsonl",
    show_default=True,
    hidden=True,
    help="Path to the task archive JSONL file.",
)
def retro(
    since: float | None,
    output: str | None,
    print_output: bool,
    archive: str,
) -> None:
    """Generate a retrospective report from completed and failed tasks.

    \b
    Reads task history from .sdd/archive/tasks.jsonl and writes a markdown
    report to .sdd/runtime/retrospective.md.

    \b
      bernstein retro                    # report on all recorded tasks
      bernstein retro --since 24         # last 24 hours only
      bernstein retro --print            # print to stdout as well
      bernstein retro -o report.md       # write to custom file
    """
    import time as _time

    from bernstein.core.retrospective import generate_retrospective

    workdir = Path.cwd()
    archive_path = Path(archive)
    runtime_dir = workdir / ".sdd" / "runtime"

    since_ts: float | None = None
    if since is not None:
        since_ts = _time.time() - since * 3600

    done_tasks, failed_tasks = _load_archive_tasks(archive_path, since_ts)

    if not done_tasks and not failed_tasks:
        label = f"in the last {since}h" if since is not None else "in the archive"
        console.print(f"[yellow]No completed or failed tasks found {label}.[/yellow]")
        console.print(f"[dim]Archive: {archive_path}[/dim]")

    collector = _build_collector_from_archive(archive_path, since_ts)

    all_ts = [t.created_at for t in done_tasks + failed_tasks if t.created_at]
    run_start_ts = min(all_ts) if all_ts else _time.time()

    # Redirect output if custom file given
    out_dir = runtime_dir
    retro_filename = "retrospective.md"
    if output:
        out_path = Path(output).resolve()
        out_dir = out_path.parent
        retro_filename = out_path.name

    generate_retrospective(
        done_tasks=done_tasks,
        failed_tasks=failed_tasks,
        collector=collector,
        runtime_dir=out_dir,
        run_start_ts=run_start_ts,
    )

    # generate_retrospective always writes to runtime_dir/retrospective.md
    actual_path = out_dir / "retrospective.md"
    if output and Path(output).name != "retrospective.md":
        # Rename to the requested filename
        actual_path.rename(out_dir / retro_filename)
        actual_path = out_dir / retro_filename

    if print_output:
        console.print(actual_path.read_text())
    else:
        console.print(f"[green]Retrospective written to[/green] {actual_path}")
        console.print(
            f"[dim]{len(done_tasks)} done, {len(failed_tasks)} failed"
            + (f", since {since}h ago" if since is not None else "")
            + "[/dim]"
        )


# ---------------------------------------------------------------------------
# help-all — progressive disclosure: full flag list
# ---------------------------------------------------------------------------


@cli.command("help-all", hidden=False)
@click.pass_context
def help_all(ctx: click.Context) -> None:
    """Show all options including advanced flags.

    \b
    Advanced flags (hidden from default --help):
      --evolve / -e             Continuous self-improvement mode
      --max-cycles N            Stop after N evolve cycles (default: unlimited)
      --budget N                Stop after $N spent (default: unlimited)
      --interval N              Seconds between evolve cycles (default: 300)
      --github                  Sync evolve proposals as GitHub Issues
      --headless                Run without TUI dashboard (for CI/overnight)
      --yes / -y                Skip cost confirmation prompt

    \b
    All subcommands:
      bernstein status          Task summary and active agents
      bernstein stop            Stop all agents and server
      bernstein doctor          Run self-diagnostics
      bernstein recap           Post-run summary (tasks, cost, duration)
      bernstein cost            Detailed spend report by model
      bernstein plan            Show full task backlog
      bernstein logs [-f]       Tail agent log output
      bernstein cancel TASK_ID  Cancel a task
      bernstein demo            Zero-to-running demo project
      bernstein retro           Generate retrospective report
      bernstein evolve          Manage self-evolution proposals
      bernstein agents          Manage agent catalogs
      bernstein benchmark       Run the golden benchmark suite
    """
    console.print(ctx.get_help())


# ---------------------------------------------------------------------------
# ideate — creative evolution pipeline
# ---------------------------------------------------------------------------


@cli.command("ideate")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show proposals without creating backlog tasks.",
)
@click.option(
    "--proposals",
    type=click.Path(exists=True),
    default=None,
    help="JSON file with pre-written visionary proposals (skip agent stage).",
)
@click.option(
    "--verdicts",
    type=click.Path(exists=True),
    default=None,
    help="JSON file with pre-written analyst verdicts (skip agent stage).",
)
@click.option(
    "--threshold",
    type=float,
    default=7.0,
    show_default=True,
    help="Minimum composite score for approval.",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def ideate(
    dry_run: bool,
    proposals: str | None,
    verdicts: str | None,
    threshold: float,
    workdir: str,
) -> None:
    """Run the creative evolution pipeline (visionary -> analyst -> tasks).

    \b
    Generates bold feature ideas, evaluates them ruthlessly, and converts
    approved proposals into backlog tasks. Requires pre-written proposal
    and verdict JSON files (agent-driven generation is a future feature).

    \b
      bernstein ideate --proposals ideas.json --verdicts evals.json
      bernstein ideate --proposals ideas.json --verdicts evals.json --dry-run
      bernstein ideate --proposals ideas.json --verdicts evals.json --threshold 8
    """
    from bernstein.evolution.creative import (
        AnalystVerdict,
        CreativePipeline,
        VisionaryProposal,
    )

    root = Path(workdir).resolve()
    state_dir = root / ".sdd"

    if not state_dir.is_dir():
        console.print(
            "[red].sdd directory not found.[/red] Run [bold]bernstein[/bold] first to initialise the workspace."
        )
        raise SystemExit(1)

    # Load proposals.
    proposal_list: list[VisionaryProposal] = []
    if proposals:
        try:
            raw = json.loads(Path(proposals).read_text(encoding="utf-8"))
            proposal_list = [VisionaryProposal.from_dict(p) for p in raw]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            console.print(f"[red]Failed to load proposals:[/red] {exc}")
            raise SystemExit(1) from exc
        console.print(f"Loaded [bold]{len(proposal_list)}[/bold] proposal(s)")

    # Load verdicts.
    verdict_list: list[AnalystVerdict] = []
    if verdicts:
        try:
            raw = json.loads(Path(verdicts).read_text(encoding="utf-8"))
            verdict_list = [AnalystVerdict.from_dict(v) for v in raw]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            console.print(f"[red]Failed to load verdicts:[/red] {exc}")
            raise SystemExit(1) from exc
        console.print(f"Loaded [bold]{len(verdict_list)}[/bold] verdict(s)")

    if not proposal_list or not verdict_list:
        console.print(
            "[yellow]Both --proposals and --verdicts are required.[/yellow]\n\n"
            "Provide JSON files with visionary proposals and analyst verdicts.\n"
            "See templates/roles/visionary/ and templates/roles/analyst/ for output formats."
        )
        raise SystemExit(1)

    pipeline = CreativePipeline(
        state_dir=state_dir,
        repo_root=root,
        approval_threshold=threshold,
    )

    result = pipeline.run(proposal_list, verdict_list, dry_run=dry_run)

    # Print results.
    if dry_run:
        console.print("\n[bold cyan][DRY RUN][/bold cyan] No backlog tasks created.\n")

    from rich.table import Table

    table = Table(
        title="Creative Pipeline Results",
        show_lines=True,
        header_style="bold cyan",
    )
    table.add_column("Proposal", min_width=25)
    table.add_column("Verdict", min_width=8)
    table.add_column("Feas.", justify="right", min_width=5)
    table.add_column("Impact", justify="right", min_width=6)
    table.add_column("Risk", justify="right", min_width=5)
    table.add_column("Score", justify="right", min_width=6)

    for v in result.verdicts:
        verdict_color = {
            "APPROVE": "green",
            "REVISE": "yellow",
            "REJECT": "red",
        }.get(v.verdict, "white")
        table.add_row(
            v.proposal_title,
            f"[{verdict_color}]{v.verdict}[/{verdict_color}]",
            f"{v.feasibility_score:.0f}",
            f"{v.impact_score:.0f}",
            f"{v.risk_score:.0f}",
            f"{v.composite_score:.1f}",
        )

    console.print(table)
    console.print(
        f"\n  Proposals: {len(result.proposals)}"
        f"  |  Approved: {len(result.approved)}"
        f"  |  Tasks created: {len(result.tasks_created)}"
    )


# ---------------------------------------------------------------------------
# install-hooks — git pre-push hook installer
# ---------------------------------------------------------------------------


@cli.command("install-hooks")
@click.option("--force", is_flag=True, default=False, help="Overwrite existing hook.")
def install_hooks(force: bool) -> None:
    """Install a pre-push git hook that runs lint and unit tests before pushing.

    \b
      bernstein install-hooks          # install hook (skip if already present)
      bernstein install-hooks --force  # overwrite existing hook
    """
    from bernstein.core.ci_fix import install_pre_push_hook

    repo_root = Path.cwd()
    installed = install_pre_push_hook(repo_root, force=force)
    hook_path = repo_root / ".git" / "hooks" / "pre-push"
    if installed:
        console.print(f"[green]Pre-push hook installed:[/green] {hook_path}")
        console.print("  Runs: ruff check + ruff format --check + pytest tests/unit/")
    else:
        console.print(f"[yellow]Hook already exists:[/yellow] {hook_path}")
        console.print("  Use --force to overwrite.")


# ---------------------------------------------------------------------------
# plugins — list discovered plugins and their hooks
# ---------------------------------------------------------------------------


@cli.command("plugins")
@click.option("--workdir", default=".", show_default=True, help="Project root to read bernstein.yaml from.")
def plugins_cmd(workdir: str) -> None:
    """List discovered plugins and the hooks they implement.

    \b
      bernstein plugins                 # discover plugins in current project
      bernstein plugins --workdir /srv  # use a different project root
    """
    from rich.table import Table

    from bernstein.plugins.manager import PluginManager

    pm = PluginManager()
    pm.load_from_workdir(Path(workdir))

    names = pm.registered_names
    if not names:
        console.print("[dim]No plugins discovered.[/dim]")
        console.print(
            "[dim]Register plugins via entry_points([/dim][cyan]'bernstein.plugins'[/cyan][dim]) "
            "or add a [/dim][cyan]plugins:[/cyan][dim] list to bernstein.yaml.[/dim]"
        )
        return

    table = Table(show_header=True, header_style="bold magenta", title="Bernstein Plugins")
    table.add_column("Plugin", style="cyan")
    table.add_column("Hooks implemented")

    for name in names:
        hooks = pm.plugin_hooks(name)
        hook_str = ", ".join(hooks) if hooks else "[dim]none[/dim]"
        table.add_row(name, hook_str)

    console.print(table)
    console.print(f"\n[dim]Total: {len(names)} plugin(s)[/dim]")


# ---------------------------------------------------------------------------
# doctor — self-diagnostics (delegates to status_cmd)
# ---------------------------------------------------------------------------


@cli.command("doctor")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.pass_context
def doctor(ctx: click.Context, as_json: bool) -> None:
    """Run self-diagnostics: check Python, adapters, API keys, port, and workspace.

    \b
      bernstein doctor          # print diagnostic report
      bernstein doctor --json   # machine-readable output
    """
    ctx.invoke(_doctor_impl, as_json=as_json)


# ---------------------------------------------------------------------------
# recap — post-run summary
# ---------------------------------------------------------------------------


@cli.command("recap")
@click.option(
    "--archive",
    default=".sdd/archive/tasks.jsonl",
    show_default=True,
    hidden=True,
    help="Path to the task archive JSONL file.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def recap(archive: str, as_json: bool) -> None:
    """Print a one-line post-run summary: tasks, pass/fail, cost, and duration.

    \b
    Reads from .sdd/archive/tasks.jsonl and .sdd/metrics/.

    \b
      bernstein recap           # human-readable summary
      bernstein recap --json    # machine-readable output
    """
    archive_path = Path(archive)

    if not archive_path.exists():
        if as_json:
            click.echo(json.dumps({"error": f"Archive not found: {archive_path}"}))
        else:
            console.print(f"[yellow]No archive found:[/yellow] {archive_path}")
            console.print("[dim]Run 'bernstein' to start, then check again after tasks complete.[/dim]")
        return

    records: list[dict[str, Any]] = []
    for line in archive_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not records:
        if as_json:
            click.echo(json.dumps({"tasks": 0, "done": 0, "failed": 0, "cost_usd": 0.0}))
        else:
            console.print("[dim]Archive is empty — no tasks have completed yet.[/dim]")
        return

    done = [r for r in records if r.get("status") == "done"]
    failed = [r for r in records if r.get("status") == "failed"]
    total = len(done) + len(failed)

    # Total cost
    cost_usd = sum(float(r.get("cost_usd") or 0.0) for r in records)

    # Also add cost from .sdd/metrics/tasks.jsonl if available
    metrics_path = archive_path.parent.parent / "metrics" / "tasks.jsonl"
    if metrics_path.exists():
        seen_tasks: set[str] = {r.get("task_id", "") for r in records}
        for line in metrics_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                tid = rec.get("task_id", "")
                if tid not in seen_tasks:
                    cost_usd += float(rec.get("cost_usd") or 0.0)
            except json.JSONDecodeError:
                continue

    # Time range
    timestamps: list[float] = []
    for r in records:
        _ts = r.get("created_at") or r.get("completed_at")
        if _ts is not None:
            timestamps.append(float(_ts))
    start_ts: float | None = min(timestamps) if timestamps else None
    _completed_ts: list[float] = []
    for r in records:
        _ct = r.get("completed_at")
        if _ct is not None:
            _completed_ts.append(float(_ct))
    end_ts: float | None = max(_completed_ts) if _completed_ts else None

    duration_s: float | None = None
    if start_ts is not None and end_ts is not None:
        duration_s = end_ts - start_ts

    if as_json:
        output: dict[str, Any] = {
            "tasks": total,
            "done": len(done),
            "failed": len(failed),
            "cost_usd": round(cost_usd, 6),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "duration_s": duration_s,
        }
        click.echo(json.dumps(output, indent=2))
        return

    # Human-readable one-liner
    start_str = ""
    end_str = ""
    if start_ts:
        start_str = datetime.datetime.fromtimestamp(float(start_ts)).strftime("%H:%M")
    if end_ts:
        end_str = datetime.datetime.fromtimestamp(float(end_ts)).strftime("%H:%M")

    dur_str = ""
    if duration_s is not None:
        m, s = divmod(int(float(duration_s)), 60)
        dur_str = f" in {m}m{s:02d}s" if m else f" in {s}s"

    cost_str = f"${cost_usd:.2f}" if cost_usd > 0 else "$0.00"

    parts: list[str] = []
    if start_str and end_str:
        parts.append(f"{start_str} → {end_str}")
    parts.append(f"{total} task(s) total")
    parts.append(f"[green]{len(done)} done[/green]")
    if failed:
        parts.append(f"[red]{len(failed)} failed[/red]")
    if dur_str:
        parts.append(dur_str.strip())
    parts.append(f"[cyan]{cost_str}[/cyan] spent")

    console.print("  ".join(parts))


# ---------------------------------------------------------------------------
# trace — view agent decision trace for a task
# ---------------------------------------------------------------------------


@cli.command("trace")
@click.argument("task_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option(
    "--traces-dir",
    default=".sdd/traces",
    show_default=True,
    hidden=True,
    help="Directory containing trace files.",
)
def trace_cmd(task_id: str, as_json: bool, traces_dir: str) -> None:
    """Show the step-by-step decision trace for TASK_ID.

    \b
    Each agent execution is recorded as a structured trace capturing:
      - Files read (orient), decisions made (plan), edits (edit), tests run (verify)
      - Model, effort, total duration, and outcome

    \b
      bernstein trace abc123          # Rich tree view
      bernstein trace abc123 --json   # Raw JSON
    """
    from bernstein.core.traces import TraceStore

    store = TraceStore(Path(traces_dir))
    traces = store.read_by_task(task_id)

    if not traces:
        # Also try treating task_id as a trace_id directly
        t = store.read_by_trace_id(task_id)
        if t is not None:
            traces = [t]

    if not traces:
        console.print(f"[yellow]No trace found for:[/yellow] {task_id}")
        console.print("[dim]Traces are written to .sdd/traces/ during agent runs.[/dim]")
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps([t.to_dict() for t in traces], indent=2))
        return

    from rich.panel import Panel
    from rich.tree import Tree

    for trace in traces:
        dur = ""
        if trace.duration_s is not None:
            m, s = divmod(int(trace.duration_s), 60)
            dur = f" • {m}m{s:02d}s" if m else f" • {s}s"

        outcome_style = {
            "success": "[green]success[/green]",
            "failed": "[red]failed[/red]",
            "unknown": "[yellow]unknown[/yellow]",
        }.get(trace.outcome, trace.outcome)

        header = f"[bold]{trace.agent_role}[/bold] agent [dim]{trace.model}/{trace.effort}[/dim]{dur} • {outcome_style}"
        tree = Tree(header)
        tree.add(f"[dim]trace_id:[/dim] {trace.trace_id}")
        tree.add(f"[dim]session:[/dim]  {trace.session_id}")
        tree.add(f"[dim]tasks:[/dim]    {', '.join(trace.task_ids)}")

        steps_node = tree.add(f"[bold cyan]steps ({len(trace.steps)})[/bold cyan]")

        STEP_STYLES: dict[str, str] = {
            "spawn": "dim",
            "orient": "blue",
            "plan": "yellow",
            "edit": "magenta",
            "verify": "cyan",
            "complete": "green",
            "fail": "red",
        }

        for step in trace.steps:
            style = STEP_STYLES.get(step.type, "white")

            # Build suffix: duration + tokens
            suffix_parts: list[str] = []
            if step.duration_ms > 0:
                if step.duration_ms >= 1000:
                    suffix_parts.append(f"[dim]{step.duration_ms // 1000}s[/dim]")
                else:
                    suffix_parts.append(f"[dim]{step.duration_ms}ms[/dim]")
            if step.tokens > 0:
                suffix_parts.append(f"[dim]{step.tokens:,} tok[/dim]")
            suffix = ("  " + "  ".join(suffix_parts)) if suffix_parts else ""

            label = f"[{style}]{step.type:8s}[/{style}]  {step.detail}{suffix}"
            snode = steps_node.add(label)
            if step.files:
                for f in step.files[:5]:
                    snode.add(f"[dim]{f}[/dim]")
                if len(step.files) > 5:
                    snode.add(f"[dim]… and {len(step.files) - 5} more[/dim]")

        # Token/cost summary across all steps
        total_tokens = sum(s.tokens for s in trace.steps)
        if total_tokens > 0:
            from bernstein.core.cost import _model_cost  # type: ignore[attr-defined]

            cost_per_1k = _model_cost(trace.model)
            est_cost = total_tokens / 1000 * cost_per_1k
            tree.add(f"[dim]tokens:[/dim]  {total_tokens:,}  [dim]est. cost:[/dim] [yellow]${est_cost:.4f}[/yellow]")

        console.print(Panel(tree, border_style="blue", expand=False))


# ---------------------------------------------------------------------------
# replay — re-run a task from a trace
# ---------------------------------------------------------------------------


@cli.command("replay")
@click.argument("trace_id")
@click.option("--model", default=None, help="Override model (opus/sonnet/haiku).")
@click.option("--effort", default=None, help="Override effort (max/high/medium/low).")
@click.option(
    "--traces-dir",
    default=".sdd/traces",
    show_default=True,
    hidden=True,
    help="Directory containing trace files.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be replayed without actually spawning an agent.",
)
def replay_cmd(
    trace_id: str,
    model: str | None,
    effort: str | None,
    traces_dir: str,
    dry_run: bool,
) -> None:
    """Re-run a task from a previous trace.

    Loads the trace identified by TRACE_ID (or task ID), then re-submits
    the same task(s) to the task server so the orchestrator picks them up.

    \b
      bernstein replay abc123                # replay with same model
      bernstein replay abc123 --model opus   # retry with better model
      bernstein replay abc123 --dry-run      # preview without spawning
    """
    from bernstein.core.traces import TraceStore

    store = TraceStore(Path(traces_dir))

    # Try trace_id first, then as task_id
    trace = store.read_by_trace_id(trace_id)
    if trace is None:
        traces = store.read_by_task(trace_id)
        if traces:
            trace = traces[-1]  # most recent

    if trace is None:
        console.print(f"[red]Trace not found:[/red] {trace_id}")
        console.print("[dim]Use 'bernstein trace <task_id>' to list available traces.[/dim]")
        raise SystemExit(1)

    effective_model = model or trace.model
    effective_effort = effort or trace.effort

    console.print(f"[bold]Replaying trace[/bold] [cyan]{trace.trace_id}[/cyan]")
    console.print(
        f"  role:    {trace.agent_role}\n"
        f"  tasks:   {', '.join(trace.task_ids)}\n"
        f"  model:   [yellow]{effective_model}[/yellow] "
        f"{'(overridden)' if model else '(original)'}\n"
        f"  effort:  [yellow]{effective_effort}[/yellow] "
        f"{'(overridden)' if effort else '(original)'}"
    )

    if not trace.task_ids:
        from bernstein.cli.errors import no_replay_tasks

        no_replay_tasks().print()
        raise SystemExit(1)

    if dry_run:
        console.print("\n[dim][dry-run] No tasks submitted.[/dim]")
        return

    # Re-submit tasks via the task server: re-open them if they exist,
    # or re-create from stored snapshots if available.
    submitted: list[str] = []
    errors: list[str] = []

    for task_id_item in trace.task_ids:
        # Find snapshot for this task (stored in trace at spawn time)
        snapshot = next(
            (s for s in trace.task_snapshots if s.get("id") == task_id_item),
            None,
        )

        # Try fetching current task from server to get fresh metadata
        current = server_get(f"/tasks/{task_id_item}")
        if current is not None:
            # Re-create as a new task (copy title/description, use new model/effort)
            src = current
        elif snapshot is not None:
            src = snapshot
        else:
            errors.append(f"{task_id_item}: not found on server and no snapshot available")
            continue

        payload: dict[str, Any] = {
            "title": f"[replay] {src.get('title', task_id_item)}",
            "description": src.get("description", ""),
            "role": src.get("role", trace.agent_role),
            "priority": src.get("priority", 2),
            "scope": src.get("scope", "medium"),
            "complexity": src.get("complexity", "medium"),
            "model": effective_model,
            "effort": effective_effort,
        }
        resp = server_post("/tasks", payload)
        if resp is not None:
            new_id = resp.get("id", "?")
            submitted.append(new_id)
        else:
            errors.append(f"{task_id_item}: failed to create replay task on server")

    if submitted:
        console.print(f"\n[green]Submitted {len(submitted)} task(s) for replay:[/green]")
        for tid in submitted:
            console.print(f"  [cyan]{tid}[/cyan]")
        console.print("[dim]Run 'bernstein run' to process them.[/dim]")

    for err in errors:
        console.print(f"[red]Error:[/red] {err}")

    if errors and not submitted:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# github — GitHub App integration commands
# ---------------------------------------------------------------------------


@cli.group("github")
def github_group() -> None:
    """GitHub App integration.

    \b
      bernstein github setup          # print setup instructions
      bernstein github test-webhook   # send a test webhook
    """


@github_group.command("setup")
def github_setup() -> None:
    """Print instructions for setting up the GitHub App."""
    console.print("\n[bold cyan]GitHub App Setup Instructions[/bold cyan]\n")
    console.print(
        "1. Go to https://github.com/settings/apps/new\n"
        "2. Fill in the App name (e.g. 'bernstein-orchestrator')\n"
        "3. Set the Webhook URL to your server's /webhooks/github endpoint\n"
        "   e.g. https://your-server.example.com/webhooks/github\n"
        "4. Generate a webhook secret and save it\n"
        "5. Under Permissions, grant:\n"
        "   - Issues: Read & Write\n"
        "   - Pull requests: Read & Write\n"
        "   - Contents: Read\n"
        "6. Subscribe to events:\n"
        "   - Issues\n"
        "   - Pull request\n"
        "   - Pull request review comment\n"
        "   - Push\n"
        "7. Create the App and note the App ID\n"
        "8. Generate a private key (PEM file)\n"
        "9. Install the App on your repository\n\n"
        "[bold]Set these environment variables:[/bold]\n\n"
        "  export GITHUB_APP_ID=<your-app-id>\n"
        "  export GITHUB_APP_PRIVATE_KEY=<path-to-pem-or-pem-string>\n"
        "  export GITHUB_WEBHOOK_SECRET=<your-webhook-secret>\n\n"
        "[dim]See deploy/github-app/README.md for detailed instructions.[/dim]"
    )


@github_group.command("test-webhook")
@click.option("--event", default="issues", show_default=True, help="GitHub event type to simulate.")
@click.option(
    "--server-url",
    default=None,
    help="Task server URL (default: $BERNSTEIN_SERVER_URL or http://localhost:8052).",
)
def github_test_webhook(event: str, server_url: str | None) -> None:
    """Send a test webhook to verify GitHub App configuration.

    Sends a synthetic webhook event to the server's /webhooks/github
    endpoint to verify that event parsing and task creation work.
    """
    import hashlib
    import hmac as hmac_mod

    url = server_url or SERVER_URL
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "test-secret")

    # Build a synthetic payload based on event type
    payloads: dict[str, dict[str, Any]] = {
        "issues": {
            "action": "opened",
            "issue": {
                "number": 9999,
                "title": "Test issue from bernstein github test-webhook",
                "body": "This is a synthetic test issue to verify webhook integration.",
                "labels": [{"name": "enhancement"}],
            },
            "repository": {"full_name": "test-owner/test-repo"},
            "sender": {"login": "bernstein-test"},
        },
        "push": {
            "ref": "refs/heads/main",
            "commits": [
                {
                    "id": "abc12345deadbeef",
                    "message": "test: synthetic push event",
                }
            ],
            "repository": {"full_name": "test-owner/test-repo"},
            "sender": {"login": "bernstein-test"},
        },
    }

    payload = payloads.get(event)
    if payload is None:
        console.print(f"[red]Unsupported test event type:[/red] {event}")
        console.print(f"[dim]Supported: {', '.join(payloads.keys())}[/dim]")
        raise SystemExit(1)

    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac_mod.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    console.print(f"[dim]Sending test {event} webhook to {url}/webhooks/github ...[/dim]")

    try:
        resp = httpx.post(
            f"{url}/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": sig,
            },
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            console.print(f"[green]Success![/green] Tasks created: {data.get('tasks_created', 0)}")
            for tid in data.get("task_ids", []):
                console.print(f"  [cyan]{tid}[/cyan]")
        else:
            console.print(f"[red]Failed:[/red] HTTP {resp.status_code} — {resp.text}")
            raise SystemExit(1)
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {url}[/red]")
        console.print("[dim]Is the Bernstein server running? Run 'bernstein' first.[/dim]")
        raise SystemExit(1) from None


# ---------------------------------------------------------------------------
# mcp — expose Bernstein as an MCP server
# ---------------------------------------------------------------------------


@cli.command("mcp")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    show_default=True,
    help="MCP transport: stdio (local IDE) or sse (remote/web).",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="SSE server host (only used with --transport sse).",
)
@click.option(
    "--port",
    default=8053,
    show_default=True,
    help="SSE server port (only used with --transport sse).",
)
@click.option(
    "--server-url",
    default=SERVER_URL,
    show_default=True,
    help="Bernstein task server URL.",
)
def mcp_server(transport: str, host: str, port: int, server_url: str) -> None:
    """Start Bernstein as an MCP server.

    \b
    stdio transport (default) -- for local IDE integration:
      bernstein mcp

    SSE transport -- for remote/web clients:
      bernstein mcp --transport sse --port 8053

    Once running, any MCP client (Cursor, Claude Code, Cline, Windsurf)
    can call bernstein_run, bernstein_status, bernstein_tasks, and more.
    """
    from bernstein.mcp.server import run_sse, run_stdio

    if transport == "sse":
        console.print(f"[dim]Starting Bernstein MCP server (SSE) on {host}:{port} ...[/dim]")
        run_sse(server_url=server_url, host=host, port=port)
    else:
        run_stdio(server_url=server_url)


# ---------------------------------------------------------------------------
# completions — shell completion scripts
# ---------------------------------------------------------------------------


@cli.command("completions")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completions(shell: str) -> None:
    """Generate shell completion script for bash, zsh, or fish.

    \b
      eval "$(bernstein completions bash)"
      eval "$(bernstein completions zsh)"
      bernstein completions fish | source
    """
    # Click 8+ ships built-in shell completion via _<PROG>_COMPLETE env vars.
    # We generate the activation snippet the user can eval.
    shell_map = {
        "bash": 'eval "$(_BERNSTEIN_COMPLETE=bash_source bernstein)"',
        "zsh": 'eval "$(_BERNSTEIN_COMPLETE=zsh_source bernstein)"',
        "fish": "_BERNSTEIN_COMPLETE=fish_source bernstein | source",
    }
    click.echo(shell_map[shell])


# ---------------------------------------------------------------------------
# quarantine — manage cross-run task quarantine
# ---------------------------------------------------------------------------


@cli.group("quarantine")
def quarantine_group() -> None:
    """Manage the cross-run task quarantine.

    Tasks that fail repeatedly (3+ times across runs) are quarantined
    so Bernstein stops re-attempting known-bad work.
    """


@quarantine_group.command("list")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    help="Bernstein project directory.",
)
@click.option("--all", "show_all", is_flag=True, default=False, help="Include expired entries.")
def quarantine_list(workdir: str, show_all: bool) -> None:
    """List quarantined tasks.

    By default shows only active (non-expired) entries.
    Use --all to include expired entries older than 7 days.
    """
    from pathlib import Path

    from bernstein.core.quarantine import QuarantineStore

    store = QuarantineStore(Path(workdir) / ".sdd" / "runtime" / "quarantine.json")
    entries = store.load() if show_all else store.get_all()

    if not entries:
        console.print("[dim]No quarantined tasks.[/dim]")
        return

    from rich.table import Table

    table = Table(title="Quarantined Tasks", show_header=True, header_style="bold red")
    table.add_column("Title", style="white", no_wrap=False)
    table.add_column("Fails", style="red", justify="right")
    table.add_column("Last Failure", style="yellow")
    table.add_column("Action", style="cyan")
    table.add_column("Reason", style="dim")

    for entry in entries:
        table.add_row(
            entry.task_title,
            str(entry.fail_count),
            entry.last_failure,
            entry.action,
            entry.reason,
        )

    console.print(table)


@quarantine_group.command("clear")
@click.argument("title", required=False, default=None)
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    help="Bernstein project directory.",
)
def quarantine_clear(title: str | None, workdir: str) -> None:
    """Remove a task from quarantine.

    \b
    Remove a specific task:
      bernstein quarantine clear "519 -- Distributed cluster mode"

    Remove all quarantined tasks:
      bernstein quarantine clear
    """
    from pathlib import Path

    from bernstein.core.quarantine import QuarantineStore

    store = QuarantineStore(Path(workdir) / ".sdd" / "runtime" / "quarantine.json")
    store.clear(title)
    if title:
        console.print(f"[green]Cleared quarantine entry for:[/green] {title}")
    else:
        console.print("[green]Cleared all quarantine entries.[/green]")
