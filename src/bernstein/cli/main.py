"""CLI entry point for Bernstein -- declarative agent orchestration.

This module defines the top-level click group and registers all
subcommand modules from:

  task_cmd.py       — task lifecycle commands (cancel, add_task, etc.)
  workspace_cmd.py  — workspace & config commands
  advanced_cmd.py   — advanced tools (trace, replay, eval, benchmark, etc.)

And existing subcommand modules:
  helpers.py    — shared constants and utility functions
  run_cmd.py    — init, run, start, demo
  stop_cmd.py   — stop (soft/hard)
  status_cmd.py — status, ps
  agents_cmd.py — agents group
  evolve_cmd.py — evolve group
  cost.py       — cost_cmd
"""

from __future__ import annotations

from pathlib import Path

import click

# Import commands from decomposed modules (NEW)
from bernstein.cli.advanced_cmd import (
    completions,
    dashboard,
    doctor,
    github_group,
    help_all,
    ideate,
    install_hooks,
    live,
    mcp_server,
    plugins_cmd,
    quarantine_group,
    recap,
    replay_cmd,
    retro,
    trace_cmd,
)

# Subcommand imports from modules
from bernstein.cli.agents_cmd import agents_group
from bernstein.cli.audit_cmd import audit_group
from bernstein.cli.chaos_cmd import chaos_group
from bernstein.cli.checkpoint_cmd import checkpoint_cmd
from bernstein.cli.ci_cmd import ci_group
from bernstein.cli.cost import cost_cmd
from bernstein.cli.eval_benchmark_cmd import (
    benchmark_group,
    eval_group,
)
from bernstein.cli.evolve_cmd import evolve
from bernstein.cli.gateway_cmd import gateway_group
from bernstein.cli.manifest_cmd import manifest_group
from bernstein.cli.quickstart_cmd import quickstart_cmd
from bernstein.cli.task_cmd import (
    add_task,
    approve,
    cancel,
    list_tasks,
    logs_cmd,
    pending,
    plan,
    reject,
    review_cmd,
    sync,
)
from bernstein.cli.verify_cmd import verify_cmd
from bernstein.cli.watch_cmd import watch_cmd
from bernstein.cli.workflow_cmd import workflow_group
from bernstein.cli.workspace_cmd import config_group, workspace_group
from bernstein.cli.wrap_up_cmd import wrap_up

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
    # Commands from task_cmd
    "add_task",
    "approve",
    "auth_headers",
    # Groups and commands from advanced_cmd
    "benchmark_group",
    "cancel",
    "chaos_group",
    "checkpoint_cmd",
    "completions",
    # Groups and commands from workspace_cmd
    "config_group",
    "console",
    "dashboard",
    "detect_available_adapter",
    "doctor",
    "eval_group",
    "find_seed_file",
    "gateway_group",
    "github_group",
    "hard_stop",
    "help_all",
    "ideate",
    "install_hooks",
    "is_alive",
    "is_process_alive",
    "kill_pid",
    "kill_pid_hard",
    "list_tasks",
    "live",
    "logs_cmd",
    "mcp_server",
    "pending",
    "plan",
    "plugins_cmd",
    "print_banner",
    "print_dry_run_table",
    "quarantine_group",
    "quickstart_cmd",
    "read_pid",
    "recap",
    "recover_orphaned_claims",
    "register_sigint_handler",
    "reject",
    "replay_cmd",
    "retro",
    "return_claimed_to_open",
    "review_cmd",
    "save_session_on_stop",
    "server_get",
    "server_post",
    "setup_demo_project",
    "sigint_handler",
    "soft_stop",
    "sync",
    "trace_cmd",
    "watch_cmd",
    "workspace_group",
    "wrap_up",
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

# ---------------------------------------------------------------------------
# Rich help
# ---------------------------------------------------------------------------


def print_rich_help() -> None:
    """Print a grouped, color-coded help screen."""
    from rich.panel import Panel
    from rich.table import Table

    c = console
    c.print()
    c.print(
        Panel(
            "[bold]bernstein[/bold]  —  declarative agent orchestration for engineering teams",
            border_style="blue",
            padding=(0, 2),
            expand=False,
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
                ("checkpoint", "Save progress for later resume"),
                ("wrap-up", "End session with summary + learnings"),
            ],
        ),
        (
            "Monitor",
            [
                ("live", "Interactive TUI dashboard (3 columns)"),
                ("dashboard", "Open web dashboard in browser"),
                ("status", "Task summary and agent health"),
                ("ps", "Running agent processes"),
                ("cost", "Spend breakdown by model and task"),
                ("logs", "Tail agent output"),
            ],
        ),
        (
            "Diagnostics",
            [
                ("doctor", "Pre-flight: adapters, API keys, ports"),
                ("recap", "Post-run: tasks, pass/fail, cost"),
                ("retro", "Detailed retrospective report"),
                ("plan", "Show task backlog"),
                ("trace [dim]ID[/dim]", "Step-by-step agent decision trace"),
            ],
        ),
        (
            "Agents & evolution",
            [
                ("agents list", "Available agents and capabilities"),
                ("agents sync", "Pull latest agent catalog"),
                ("agents discover", "Auto-detect installed CLI agents"),
                ("evolve", "Self-improvement proposals"),
                ("demo", "Zero-to-running demo in 60 seconds"),
                ("quickstart", "Zero-config demo: 3 tasks on a Flask TODO API"),
            ],
        ),
    ]
    for group_name, commands in groups:
        table = Table(show_header=False, box=None, padding=(0, 1), expand=False, pad_edge=False)
        table.add_column("", width=2)  # left indent
        table.add_column("Command", style="bold green", no_wrap=True, width=26)
        table.add_column("Description", style="dim")
        for cmd, desc in commands:
            table.add_row("", cmd, desc)
        c.print(f"  [bold]{group_name}[/bold]")
        c.print(table)
        c.print()

    c.print("  [bold]Options[/bold]")
    opts = Table(show_header=False, box=None, padding=(0, 1), expand=False, pad_edge=False)
    opts.add_column("", width=2)  # left indent
    opts.add_column("Flag", style="yellow", no_wrap=True, width=26)
    opts.add_column("", style="dim")
    opts.add_row("", "--budget [dim]N[/dim]", "Cost cap in USD (0 = unlimited)")
    opts.add_row("", "--dry-run", "Preview task plan without spawning")
    opts.add_row("", "--plan-only", "Show execution plan without running agents")
    opts.add_row("", "--from-plan [dim]path[/dim]", "Execute a saved plan file")
    opts.add_row("", "--auto-approve", "Skip confirmation prompt before execution")
    opts.add_row("", "--approval [dim]auto|review|pr[/dim]", "Gate before merge")
    opts.add_row("", "--fresh", "Ignore saved session, start clean")
    opts.add_row("", "--version", "Show version")
    c.print(opts)
    c.print("\n  [dim]Docs:[/dim] https://chernistry.github.io/bernstein/")
    c.print("  [dim]Repo:[/dim] https://github.com/chernistry/bernstein\n")


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


class _RichGroup(click.Group):
    """Click group that renders help with Rich instead of plain text."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        print_rich_help()

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
@click.option("--plan-only", is_flag=True, default=False, help="Show execution plan without running agents.")
@click.option(
    "--from-plan",
    "from_plan",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Execute a saved plan file (skips interactive planning).",
)
@click.option("--auto-approve", is_flag=True, default=False, help="Skip confirmation prompt before execution.")
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
@click.option(
    "--workflow",
    "workflow_mode",
    type=click.Choice(["governed"], case_sensitive=False),
    default=None,
    help="Activate governed workflow mode (deterministic phase-based execution).",
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
    workflow_mode: str | None,
    plan_only: bool,
    from_plan: str | None,
    auto_approve: bool,
) -> None:
    """Declarative agent orchestration for engineering teams."""
    if ctx.invoked_subcommand is not None:
        return

    from bernstein.cli.splash import splash

    seed_path = find_seed_file()
    workdir = Path.cwd()
    port = 8052

    # Detect agents for splash screen
    _splash_agents: list[dict[str, object]] = []
    try:
        from bernstein.core.agent_discovery import discover_agents_cached

        _disc = discover_agents_cached()
        _splash_agents = [
            {"name": a.name, "logged_in": a.logged_in, "default_model": a.default_model} for a in _disc.agents
        ]
    except Exception:
        pass

    # Count backlog tasks
    _task_count = 0
    try:
        _open_dir = workdir / ".sdd" / "backlog" / "open"
        if _open_dir.exists():
            _task_count = sum(1 for f in _open_dir.iterdir() if f.suffix == ".md")
    except Exception:
        pass

    # Get version
    _version = ""
    try:
        from importlib.metadata import version as _get_version

        _version = _get_version("bernstein")
    except Exception:
        pass

    # Read goal from seed
    _goal_preview = goal or ""
    if not _goal_preview and seed_path:
        try:
            import yaml

            with open(seed_path) as f:
                _seed_data = yaml.safe_load(f)
            _goal_preview = str(_seed_data.get("goal", ""))[:80]
        except Exception:
            pass

    splash(
        console,
        version=_version,
        agents=_splash_agents,  # type: ignore[arg-type]
        seed_file=str(seed_path) if seed_path else None,
        goal_preview=_goal_preview,
        budget=budget,
        task_count=_task_count,
    )

    if dry_run:
        print_dry_run_table(workdir)
        return

    # Recover orphaned claimed tickets from any prior crashed/stopped session
    recovered = recover_orphaned_claims()
    if recovered:
        console.print(f"[yellow]Recovered {recovered} orphaned ticket(s).[/yellow]")

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
        cycles_str = f"{max_cycles} cycles" if max_cycles > 0 else "unlimited"
        console.print(f"\n[bold yellow]Evolve mode (safety limit: {budget_str} cost, {cycles_str})[/bold yellow]")
        console.print(
            "[dim]Bernstein will autonomously[/dim] "
            "[bold]read your codebase, propose changes, and commit them to main[/bold][dim].\n"
        )
        if not click.confirm("Ready to enable self-improvement?"):
            console.print("[dim]Cancelled.[/dim]")
            raise SystemExit(0)

    # Main orchestration flow — call run's callback directly with mapped params
    assert run.callback is not None
    run.callback(
        goal=goal,
        seed_file=str(seed_path) if seed_path else None,
        port=port,
        cells=1,
        remote=False,
        cli=cli_override,
        model=model_override,
        workflow=workflow_mode,
        routing=None,
        compliance=None,
        container=False,
        container_image=None,
        two_phase_sandbox=False,
        plan_only=plan_only,
        from_plan=Path(from_plan) if from_plan else None,
        auto_approve=auto_approve or yes,
    )


# ---------------------------------------------------------------------------
# Register commands and groups with main CLI
# ---------------------------------------------------------------------------

# From task_cmd module - all registered with @click.command()
cli.add_command(cancel)
cli.add_command(add_task, "add-task")
cli.add_command(sync)
cli.add_command(review_cmd, "review")
cli.add_command(approve)
cli.add_command(reject)
cli.add_command(pending)
cli.add_command(plan)
cli.add_command(logs_cmd, "logs")
cli.add_command(list_tasks, "list-tasks")

# From workspace_cmd module - groups and commands
cli.add_command(workspace_group)
cli.add_command(config_group)

# From advanced_cmd module - groups and commands
cli.add_command(benchmark_group)
cli.add_command(eval_group)
cli.add_command(dashboard)
cli.add_command(live)
cli.add_command(trace_cmd, "trace")
cli.add_command(replay_cmd, "replay")
cli.add_command(github_group)
cli.add_command(mcp_server, "mcp")
cli.add_command(completions)
cli.add_command(quarantine_group)
cli.add_command(ideate)
cli.add_command(install_hooks, "install-hooks")
cli.add_command(plugins_cmd, "plugins")
cli.add_command(doctor)
cli.add_command(recap)
cli.add_command(retro)
cli.add_command(help_all, "help-all")

# Already registered elsewhere
cli.add_command(agents_group)
cli.add_command(evolve)
cli.add_command(cost_cmd, "cost")
cli.add_command(status)
cli.add_command(ps_cmd, "ps")
cli.add_command(stop)
cli.add_command(init)
cli.add_command(start)
cli.add_command(demo)
cli.add_command(checkpoint_cmd, "checkpoint")
cli.add_command(wrap_up, "wrap-up")
cli.add_command(audit_group, "audit")
cli.add_command(verify_cmd, "verify")
cli.add_command(chaos_group, "chaos")
cli.add_command(manifest_group, "manifest")
cli.add_command(ci_group, "ci")
cli.add_command(gateway_group, "gateway")
cli.add_command(workflow_group, "workflow")
cli.add_command(quickstart_cmd, "quickstart")
cli.add_command(watch_cmd, "watch")
