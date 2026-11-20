"""CLI entry point for Bernstein -- the open-source governance layer for AI agents.

This module defines the top-level click group and registers all
subcommand modules from:

  task_cmd.py       - task lifecycle commands (cancel, add_task, etc.)
  workspace_cmd.py  - workspace & config commands
  advanced_cmd.py   - advanced tools (trace, replay, eval, benchmark, etc.)

And existing subcommand modules:
  helpers.py    - shared constants and utility functions
  run_cmd.py    - init, run, start, demo
  stop_cmd.py   - stop (soft/hard)
  status_cmd.py - status, ps
  agents_cmd.py - agents group
  evolve_cmd.py - evolve group
  cost.py       - cost_cmd
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, MutableMapping
from functools import cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click

from bernstein.cli.aliases import expand_alias
from bernstein.cli.verbosity import apply_verbosity
from bernstein.core.observability.json_logging import setup_json_logging

if TYPE_CHECKING:
    from bernstein.cli.advanced_cmd import dashboard as dashboard
    from bernstein.cli.advanced_cmd import doctor as doctor
    from bernstein.cli.advanced_cmd import github_group as github_group
    from bernstein.cli.advanced_cmd import help_all as help_all
    from bernstein.cli.advanced_cmd import install_hooks as install_hooks
    from bernstein.cli.advanced_cmd import live as live
    from bernstein.cli.advanced_cmd import mcp_server as mcp_server
    from bernstein.cli.advanced_cmd import plugins_cmd as plugins_cmd
    from bernstein.cli.advanced_cmd import quarantine_group as quarantine_group
    from bernstein.cli.advanced_cmd import recap as recap
    from bernstein.cli.advanced_cmd import replay_cmd as replay_cmd
    from bernstein.cli.advanced_cmd import retro as retro
    from bernstein.cli.advanced_cmd import trace_cmd as trace_cmd
    from bernstein.cli.auth_cmd import auth_group as auth_group
    from bernstein.cli.auth_cmd import auth_login as auth_login
    from bernstein.cli.changelog_cmd import changelog_group as changelog_cmd
    from bernstein.cli.chaos_cmd import chaos_group as chaos_group
    from bernstein.cli.cloud_cmd import cloud_group as cloud_group
    from bernstein.cli.commands.adapter_cmd import adapters_group, test_adapter
    from bernstein.cli.commands.autofix_cmd import autofix_group as autofix_group
    from bernstein.cli.commands.cache_cmd import cache_group as cache_group
    from bernstein.cli.commands.checkpoint_cmd import checkpoint_cmd as checkpoint_cmd
    from bernstein.cli.commands.completions_cmd import completions_cmd as completions
    from bernstein.cli.commands.gateway_cmd import gateway_group as gateway_group
    from bernstein.cli.commands.integrations_cmd import integrations_group as integrations_group
    from bernstein.cli.commands.resume_cmd import resume_cmd as resume_cmd
    from bernstein.cli.commands.security_review_cmd import security_review_cmd as security_review_cmd
    from bernstein.cli.commands.worker_cmd import worker as worker
    from bernstein.cli.debug_cmd import debug_cmd as debug_cmd
    from bernstein.cli.diff_cmd import diff_cmd as diff_cmd
    from bernstein.cli.eval_benchmark_cmd import benchmark_alias_group as benchmark_alias_group
    from bernstein.cli.eval_benchmark_cmd import benchmark_group as benchmark_group
    from bernstein.cli.eval_benchmark_cmd import eval_group as eval_group
    from bernstein.cli.maintenance_cmd import cleanup_cmd as cleanup_cmd
    from bernstein.cli.maintenance_cmd import history_cmd as history_cmd
    from bernstein.cli.merge_cmd import merge_cmd as merge_cmd
    from bernstein.cli.quickstart_cmd import quickstart_alias_cmd as quickstart_alias_cmd
    from bernstein.cli.run_changelog_cmd import run_changelog_default as run_changelog_cmd
    from bernstein.cli.run_changelog_cmd import run_changelog_default as run_changelog_default
    from bernstein.cli.run_cmd import DEMO_TASKS as DEMO_TASKS
    from bernstein.cli.run_cmd import detect_available_adapter as detect_available_adapter
    from bernstein.cli.run_cmd import setup_demo_project as setup_demo_project
    from bernstein.cli.scaffold_cmd import scaffold_cmd as scaffold_cmd
    from bernstein.cli.stop_cmd import hard_stop as hard_stop
    from bernstein.cli.stop_cmd import recover_orphaned_claims as recover_orphaned_claims
    from bernstein.cli.stop_cmd import register_sigint_handler as register_sigint_handler
    from bernstein.cli.stop_cmd import return_claimed_to_open as return_claimed_to_open
    from bernstein.cli.stop_cmd import save_session_on_stop as save_session_on_stop
    from bernstein.cli.stop_cmd import sigint_handler as sigint_handler
    from bernstein.cli.stop_cmd import soft_stop as soft_stop
    from bernstein.cli.stop_cmd import write_shutdown_signals as write_shutdown_signals
    from bernstein.cli.task_cmd import add_task as add_task
    from bernstein.cli.task_cmd import approve as approve
    from bernstein.cli.task_cmd import backlog_group as backlog_group
    from bernstein.cli.task_cmd import cancel as cancel
    from bernstein.cli.task_cmd import list_tasks as list_tasks
    from bernstein.cli.task_cmd import logs_cmd as logs_cmd
    from bernstein.cli.task_cmd import pending as pending
    from bernstein.cli.task_cmd import plan as plan
    from bernstein.cli.task_cmd import reject as reject
    from bernstein.cli.task_cmd import review_cmd as review_cmd
    from bernstein.cli.task_cmd import sync as sync
    from bernstein.cli.templates_cmd import templates_group as templates_group
    from bernstein.cli.voice_cmd import listen_cmd as listen_cmd
    from bernstein.cli.watch_cmd import watch_cmd as watch_cmd
    from bernstein.cli.wiki_cmd import wiki_group as wiki_group
    from bernstein.cli.workspace_cmd import config_group as config_group
    from bernstein.cli.workspace_cmd import workspace_group as workspace_group
    from bernstein.cli.worktrees_cmd import worktrees_group as worktrees_group
    from bernstein.cli.wrap_up_cmd import wrap_up as wrap_up
    from bernstein.eval.bench.bench_cli import bench_group as bench_group

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
    "adapters_group",
    "add_task",
    "approve",
    "auth_group",
    "auth_headers",
    "auth_login",
    # Groups and commands from advanced_cmd
    "backlog_group",
    "benchmark_alias_group",
    "benchmark_group",
    "cache_group",
    "cancel",
    "changelog_cmd",
    "chaos_group",
    "checkpoint_cmd",
    "cleanup_cmd",
    "cloud_group",
    "completions",
    # Groups and commands from workspace_cmd
    "config_group",
    "console",
    "dashboard",
    "debug_cmd",
    "detect_available_adapter",
    "diff_cmd",
    "doctor",
    "eval_group",
    "find_seed_file",
    "gateway_group",
    "github_group",
    "hard_stop",
    "help_all",
    "history_cmd",
    "install_hooks",
    "is_alive",
    "is_process_alive",
    "kill_pid",
    "kill_pid_hard",
    "list_tasks",
    "listen_cmd",
    "live",
    "logs_cmd",
    "mcp_server",
    "merge_cmd",
    "pending",
    "plan",
    "plugins_cmd",
    "print_banner",
    "print_dry_run_table",
    "quarantine_group",
    "quickstart_alias_cmd",
    "read_pid",
    "recap",
    "recover_orphaned_claims",
    "register_sigint_handler",
    "reject",
    "replay_cmd",
    "retro",
    "return_claimed_to_open",
    "review_cmd",
    # #3142: ``run_changelog_cmd`` was renamed to ``run_changelog_default`` and
    # the click command moved inside the ``changelog`` group. Re-export under
    # the old name so any out-of-tree import (``from bernstein.cli.main import
    # run_changelog_cmd``) keeps working for as long as the deprecated alias
    # lives; both go away together in a release after the 4.0 line.
    "run_changelog_cmd",
    "run_changelog_default",
    "save_session_on_stop",
    "scaffold_cmd",
    "security_review_cmd",
    "server_get",
    "server_post",
    "setup_demo_project",
    "sigint_handler",
    "soft_stop",
    "sync",
    "templates_group",
    "test_adapter",
    "trace_cmd",
    "watch_cmd",
    "wiki_group",
    "worker",
    "workspace_group",
    "worktrees_group",
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
    adapter_cli_choice,
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

# ---------------------------------------------------------------------------
# Error telemetry bootstrap
# ---------------------------------------------------------------------------


def _init_error_telemetry() -> None:
    """Initialise the operator-managed error-telemetry sink, if configured.

    Wires sentry-sdk to a Sentry-protocol-compatible error sink whose DSN is
    supplied via the portable ``BERNSTEIN_TELEMETRY_DSN``.  This is the single,
    host-agnostic contract documented in
    ``docs/observability/side-channel.md``.

    The function is a no-op when:

    * ``BERNSTEIN_TELEMETRY_DSN`` is not set (operator has not yet stood up the
      sink), or
    * the ``sentry-sdk`` package is not importable (minimal install without
      the ``observability`` extra).

    Sample rates are set to zero, so no events fire on the happy path -- only
    explicit ``sentry_sdk.capture_*`` calls and unhandled exceptions reach the
    sink.  ``send_default_pii`` is disabled so no operator identifiers leave
    the process unless captured explicitly.
    """
    import os

    dsn = os.environ.get("BERNSTEIN_TELEMETRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk  # type: ignore[import-not-found]
    except ImportError:
        return
    from bernstein import __version__

    # ``sentry_sdk.init`` raises ``BadDsn`` for a malformed DSN. Telemetry
    # is best-effort: a misconfigured DSN must not crash the CLI on import.
    try:
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,
            profiles_sample_rate=0.0,
            send_default_pii=False,
            release=__version__,
        )
    except Exception:
        # Best-effort import-time telemetry init: never crash the CLI.
        return


_init_error_telemetry()


# ---------------------------------------------------------------------------
# Rich help
# ---------------------------------------------------------------------------


def print_rich_help() -> None:
    """Print a grouped, color-coded help screen."""
    from rich.panel import Panel
    from rich.table import Table

    from bernstein.adapters.registry import advertised_adapter_names

    registered, plugins = advertised_adapter_names()
    adapter_count = f"{len(registered)} selectable adapter names"
    if plugins:
        adapter_count += f" and {len(plugins)} additional plugin declarations"

    c = console
    c.print()
    c.print(
        Panel(
            "[bold]bernstein[/bold]  the open-source governance layer for AI agents.\n"
            "  No model in the coordination loop, so a plan replays to the same task graph.\n"
            f"  {adapter_count}, per-task git worktrees, "
            "opt-in HMAC-SHA256 audit chain (RFC 2104).",
            border_style="blue",
            padding=(0, 2),
            expand=False,
        )
    )
    c.print("\n  [bold cyan]quick start[/bold cyan]")
    c.print('  [dim]$[/dim] bernstein -g [green]"Add JWT auth with tests"[/green]     [dim]# inline goal[/dim]')
    c.print("  [dim]$[/dim] bernstein                                    [dim]# from bernstein.yaml[/dim]")
    c.print("  [dim]$[/dim] bernstein run plan.yaml                      [dim]# execute a plan file[/dim]")
    c.print("  [dim]$[/dim] bernstein init                               [dim]# set up a new project[/dim]")
    c.print()

    groups: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "run & manage",
            [
                ("bernstein -g [dim]GOAL[/dim]", "orchestrate agents for an inline goal"),
                ("bernstein", "run from bernstein.yaml or backlog"),
                ("run [dim]plan.yaml[/dim]", "execute a plan file (stages + steps)"),
                ("init", "initialize project (.sdd/ + bernstein.yaml)"),
                ("start", "start server + spawn manager, detached (cluster central node)"),
                ("serve", "run the task server in the foreground (container / node)"),
                ("stop", "graceful stop (agents save work first)"),
                ("stop --force", "hard stop (kill immediately)"),
                ("checkpoint", "save progress for later resume"),
                ("wrap-up", "end session with summary + learnings"),
            ],
        ),
        (
            "monitor",
            [
                ("live", "interactive TUI dashboard (2 columns + activity panel)"),
                ("status", "task summary and agent health"),
                ("ps", "running agent processes"),
                ("cost", "spend breakdown by model and task"),
                ("logs", "tail agent output"),
            ],
        ),
        (
            "diagnostics",
            [
                ("doctor", "pre-flight: adapters, API keys, ports"),
                ("recap", "post-run: tasks, pass/fail, cost"),
                ("retro", "detailed retrospective report"),
                ("plan", "show task backlog"),
                ("trace [dim]ID[/dim]", "step-by-step agent decision trace"),
            ],
        ),
        (
            "agents & evolution",
            [
                ("agents list", "available agents and capabilities"),
                ("agents sync", "pull latest agent catalog"),
                ("agents discover", "auto-detect installed CLI agents"),
                ("worker", "join a cluster as a remote worker node"),
                ("evolve", "self-improvement proposals"),
                ("demo", "zero-to-running demo in 60 seconds"),
                ("demo --flask-todo", "3 tasks on a Flask TODO API"),
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

    c.print("  [bold]options[/bold]")
    opts = Table(show_header=False, box=None, padding=(0, 1), expand=False, pad_edge=False)
    opts.add_column("", width=2)  # left indent
    opts.add_column("Flag", style="yellow", no_wrap=True, width=26)
    opts.add_column("", style="dim")
    opts.add_row("", "--budget [dim]N[/dim]", "cost cap in USD (0 = unlimited)")
    opts.add_row("", "--dry-run", "preview task plan without spawning")
    opts.add_row("", "--plan-only", "show execution plan without running agents")
    opts.add_row("", "--from-plan [dim]path[/dim]", "execute a saved plan file")
    opts.add_row("", "--auto-approve", "skip confirmation prompt before execution")
    opts.add_row("", "--approval [dim]auto|review|pr[/dim]", "gate before merge")
    opts.add_row("", "--fresh", "ignore saved session, start clean")
    opts.add_row("", "--version", "show version")
    c.print(opts)
    c.print("\n  [dim]docs:[/dim] https://docs.bernstein.run/en/latest/")
    c.print("  [dim]repo:[/dim] https://github.com/sipyourdrink-ltd/bernstein")
    c.print("  [dim]audit chain:[/dim] docs/security/audit-log.md  (RFC 2104 HMAC-SHA256)\n")


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


type _CommandTarget = tuple[str, str]


# Additional subcommands historically registered by the CLI root.
_CHILDREN: dict[str, tuple[tuple[str, str, str], ...]] = {
    "plan": (
        ("generate", "bernstein.cli.plan_generate_cmd", "plan_generate"),
        ("compile", "bernstein.cli.plan_compile_cmd", "plan_compile"),
        ("ls", "bernstein.cli.plan_archive_cmd", "plan_ls"),
        ("show", "bernstein.cli.plan_archive_cmd", "plan_show"),
        ("dag", "bernstein.cli.plan_dag_cmd", "plan_dag"),
        ("validate", "bernstein.cli.plan_validate_cmd", "validate_plan"),
    ),
    "mcp_server": (("catalog", "bernstein.cli.commands.mcp_catalog_cmd", "catalog_group"),),
    "skills_group": (
        ("catalog", "bernstein.cli.commands.skills_catalog_cmd", "catalog_group"),
        ("package", "bernstein.cli.commands.skills_package_cmd", "package_group"),
    ),
    "auth_group": (("dashboard-token", "bernstein.cli.commands.dashboard_token_cmd", "dashboard_token_group"),),
    "report_cmd": (
        ("postmortem", "bernstein.cli.postmortem_cmd", "postmortem_cmd"),
        ("incident", "bernstein.cli.incident_cmd", "incident_cmd"),
        ("commits", "bernstein.cli.status_cmd", "commit_stats_cmd"),
    ),
    "worktrees_group": (("graph", "bernstein.cli.commands.run_graph_cmd", "run_graph_cmd"),),
}


@cache
def _load_command(target: _CommandTarget) -> click.Command:
    module, attribute = target
    command = cast("click.Command", getattr(import_module(module), attribute))
    if isinstance(command, click.Group):
        for name, child_module, child in _CHILDREN.get(attribute, ()):
            command.add_command(_load_command((child_module, child)), name)
    return command


class _CommandRegistry(MutableMapping[str, click.Command]):
    """Keep command names available before their modules are imported."""

    def __init__(self) -> None:
        self._entries: dict[str, click.Command | _CommandTarget] = {}
        self.exports: dict[str, _CommandTarget] = {}

    def defer(self, name: str, module: str, attribute: str, *, export_name: str | None = None) -> None:
        target = (module, attribute)
        self._entries[name] = target
        self.exports[export_name or attribute] = target

    def short_help(self, name: str) -> str | None:
        entry = self._entries[name]
        if isinstance(entry, tuple):
            metadata = _completion_metadata()
            if name in metadata:
                return metadata[name]
            entry = self[name]
        return None if entry.hidden else entry.get_short_help_str()

    def __getitem__(self, name: str) -> click.Command:
        entry = self._entries[name]
        return _load_command(entry) if isinstance(entry, tuple) else entry

    def __setitem__(self, name: str, command: click.Command) -> None:
        self._entries[name] = command

    def __delitem__(self, name: str) -> None:
        del self._entries[name]

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


_commands = _CommandRegistry()


# Helpers previously imported by this module remain available to callers.
_EXPORTS = {
    "DEMO_TASKS": ("bernstein.cli.run_cmd", "DEMO_TASKS"),
    "benchmark_group": ("bernstein.cli.eval_benchmark_cmd", "benchmark_group"),
    "detect_available_adapter": ("bernstein.cli.run_cmd", "detect_available_adapter"),
    "hard_stop": ("bernstein.cli.stop_cmd", "hard_stop"),
    "logs_cmd": ("bernstein.cli.task_cmd", "logs_cmd"),
    "recover_orphaned_claims": ("bernstein.cli.stop_cmd", "recover_orphaned_claims"),
    "register_sigint_handler": ("bernstein.cli.stop_cmd", "register_sigint_handler"),
    "return_claimed_to_open": ("bernstein.cli.stop_cmd", "return_claimed_to_open"),
    "run_changelog_cmd": ("bernstein.cli.run_changelog_cmd", "run_changelog_default"),
    "run_changelog_default": ("bernstein.cli.run_changelog_cmd", "run_changelog_default"),
    "save_session_on_stop": ("bernstein.cli.stop_cmd", "save_session_on_stop"),
    "setup_demo_project": ("bernstein.cli.run_cmd", "setup_demo_project"),
    "sigint_handler": ("bernstein.cli.stop_cmd", "sigint_handler"),
    "soft_stop": ("bernstein.cli.stop_cmd", "soft_stop"),
    "write_shutdown_signals": ("bernstein.cli.stop_cmd", "write_shutdown_signals"),
}


def __getattr__(name: str) -> Any:
    target = _commands.exports.get(name)
    if target is not None:
        return _load_command(target)
    if name in _EXPORTS:
        module, attribute = _EXPORTS[name]
        return getattr(import_module(module), attribute)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(globals().keys() | _commands.exports.keys() | _EXPORTS.keys())


@cache
def _completion_metadata() -> dict[str, str | None]:
    import json

    return json.loads(Path(__file__).with_name("completion_metadata.json").read_text(encoding="utf-8"))


class _RichGroup(click.Group):
    """Click group that renders help with Rich instead of plain text.

    Also resolves short aliases  so ``bernstein s`` maps to
    ``bernstein status``, etc.
    """

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        print_rich_help()

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        # Resolve alias first. An alias value may carry flags (``i`` ->
        # ``init --wizard``), so only its first token is a command name.
        tokens = expand_alias(cmd_name)
        if tokens is not None:
            return super().get_command(ctx, tokens[0])
        return super().get_command(ctx, cmd_name)

    def shell_complete(self, ctx: click.Context, incomplete: str) -> list[Any]:
        from click.shell_completion import CompletionItem

        results = []
        for name in self.list_commands(ctx):
            if name.startswith(incomplete):
                alias = expand_alias(name)
                target = alias[0] if alias else name
                if target in _commands:
                    help_text = _commands.short_help(target)
                    if help_text is not None:
                        results.append(CompletionItem(name, help=help_text))
        results.extend(click.Command.shell_complete(self, ctx, incomplete))
        return results

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args:
            tokens = expand_alias(args[0])
            if tokens is not None:
                args = [*tokens, *args[1:]]
        if args and args[0] == "run":
            self._announce_run(ctx, args[1:])
        return super().resolve_command(ctx, args)

    @staticmethod
    def _announce_run(ctx: click.Context, args: list[str]) -> None:
        if ctx.resilient_parsing or not sys.stdout.isatty():
            return
        if any(ctx.params.get(flag) for flag in ("quiet", "headless", "output_json")):
            return
        if ctx.params.get("output_format") == "json" or {"--help", "--quiet", "--json"}.intersection(args):
            return
        ctx.ensure_object(dict)
        if not ctx.obj.get("_BANNER_PRINTED"):
            preview = (
                ctx.params.get("plan_only")
                or ctx.params.get("dry_run")
                or {"--plan-only", "--dry-run"}.intersection(args)
            )
            click.echo("Bernstein · Preparing run preview" if preview else "Bernstein · Preparing local run")
            ctx.obj["_BANNER_PRINTED"] = True

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Intercept ``--help-all`` so it works as both option and subcommand."""
        if "--help-all" in args:
            args = ["help-all"]
        return super().parse_args(ctx, args)


def _validate_evolve_mode(evolve: bool, budget: float, max_cycles: int, yes: bool) -> None:
    """Validate evolve mode flags: require safety limit and user confirmation."""
    if not evolve:
        return
    if budget <= 0 and max_cycles <= 0:
        from bernstein.cli.errors import BernsteinError

        BernsteinError(
            what="Evolve mode requires a safety limit",
            why="Evolve mode will autonomously modify code indefinitely",
            fix="Add --budget 5.00 or --max-cycles 10",
        ).print()
        raise SystemExit(1)

    if not yes:
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


@click.group(cls=_RichGroup, commands=_commands, invoke_without_command=True)
@click.version_option(package_name="bernstein")
@click.option("--goal", "-g", default=None, help="Inline goal (no seed file needed).")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output in JSON format.")
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["json", "text"]),
    default=None,
    help="Output format: json for machine-readable, text for Rich (default).",
)
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
    type=adapter_cli_choice(),
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
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show debug-level output.")
@click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress all non-error output.")
@click.option(
    "--task",
    "-t",
    "task_filter",
    default=None,
    metavar="PATTERN",
    help="Run only backlog tasks matching PATTERN (e.g. 'gh-62' or 'mutant-fish').",
)
@click.option(
    "--auto-pr",
    is_flag=True,
    default=False,
    help="Automatically create a GitHub PR when all tasks complete.",
)
@click.option(
    "--activity-log",
    "activity_log_path",
    is_flag=False,
    flag_value=".sdd/logs/activity.log",
    default=None,
    help="Write activity to a log file. Default: .sdd/logs/activity.log.",
)
@click.option(
    "--sandbox",
    "sandbox_override",
    default=None,
    type=click.Choice(
        ["docker", "podman", "worktree", "e2b", "modal", "daytona", "blaxel", "runloop", "vercel"],
        case_sensitive=False,
    ),
    help=(
        "Sandbox backend (overrides selector). Free: worktree, docker, podman. "
        "Paid (require --allow-paid): e2b, modal, daytona, blaxel, runloop, vercel."
    ),
)
@click.option(
    "--allow-paid",
    "allow_paid",
    is_flag=True,
    default=False,
    help="Allow the sandbox selector to consider paid cloud backends.",
)
@click.option(
    "--refine",
    "refine_spec",
    default=None,
    metavar="SPEC",
    help=(
        "Iterative self-refinement loop (issue #1403). Format: 'rounds:N,critic:adversary,stop:plateau,threshold:0.9'."
    ),
)
@click.option(
    "--unsafe-allow-unicode-tags",
    "unsafe_allow_unicode_tags",
    is_flag=True,
    default=False,
    hidden=True,
    help=(
        "Disable the skill-pack invisible-Unicode sanitizer (DANGEROUS - only for "
        "reproducing a poisoned-skill incident; see templates/skills/README.md)."
    ),
)
@click.pass_context
def cli(
    ctx: click.Context,
    goal: str | None,
    output_json: bool,
    output_format: str | None,
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
    verbose: bool,
    quiet: bool,
    task_filter: str | None,
    auto_pr: bool,
    activity_log_path: str | None,
    sandbox_override: str | None,
    allow_paid: bool,
    refine_spec: str | None,
    unsafe_allow_unicode_tags: bool,
) -> None:
    """The open-source governance layer for AI agents.

    Parallel runs in per-task git worktrees, byte-identical run receipts, signed
    lineage that checks offline from the artefacts. Replaying the HMAC audit
    chain additionally needs the install audit key.
    """
    # The skill-pack invisible-Unicode sanitizer reads its opt-out from this
    # env var; set it as early as possible so any later import that triggers a
    # SkillLoader sees the operator's choice. Default is OFF (sanitize on).
    if unsafe_allow_unicode_tags:
        import os

        os.environ["BERNSTEIN_UNSAFE_ALLOW_UNICODE_TAGS"] = "1"
    setup_json_logging()
    ctx.ensure_object(dict)
    # --json flag or --output json both enable JSON output mode
    ctx.obj["JSON"] = output_json or (output_format == "json")
    # Apply --verbose / --quiet
    if verbose and quiet:
        raise click.UsageError("Cannot use --verbose and --quiet together.")
    # Propagate the verbosity flag so the first-run hint guard can decide
    # whether to render the original traceback below the hint panel.
    ctx.obj["VERBOSE"] = verbose
    ctx.obj["QUIET"] = quiet
    ctx.obj["HEADLESS"] = headless
    apply_verbosity(verbose, quiet)

    # Parse the iterative-refinement spec (issue #1403). Failing fast on a
    # malformed value matches Click's other parser behaviours and surfaces
    # operator typos before any agent invocation cost is incurred.
    if refine_spec:
        from bernstein.core.orchestration.refinement_loop import parse_refine_spec

        try:
            ctx.obj["REFINE_SPEC"] = parse_refine_spec(refine_spec)
        except ValueError as exc:
            raise click.UsageError(f"Invalid --refine spec: {exc}") from exc
    else:
        ctx.obj["REFINE_SPEC"] = None

    if ctx.invoked_subcommand is not None:
        return

    _RichGroup._announce_run(ctx, [])
    from bernstein.cli.splash_screen import splash

    seed_path = find_seed_file()
    workdir = Path.cwd()
    port = 8052

    show_splash = not (ctx.obj["JSON"] or quiet or headless or ctx.obj.get("_BANNER_PRINTED"))
    if show_splash and splash(
        console,
        version="",
        agents=[],
        seed_file=str(seed_path) if seed_path else None,
        goal_preview=goal or "",
        budget=budget,
        task_count=0,
        skip_animation=True,
    ):
        ctx.obj["_BANNER_PRINTED"] = True

    if dry_run:
        # Validate the seed with the same rules the real run uses before
        # rendering the preview, so --dry-run cannot report success on a
        # seed the run would reject (issue #2785).
        from bernstein.cli.run_preflight import validate_seed_or_exit

        validate_seed_or_exit(str(seed_path) if seed_path else None)
        print_dry_run_table(workdir)
        return

    # Recover orphaned claimed tickets from any prior crashed/stopped session
    from bernstein.cli.stop_cmd import recover_orphaned_claims

    recovered = recover_orphaned_claims()
    if recovered:
        console.print(f"[yellow]Recovered {recovered} orphaned ticket(s).[/yellow]")

    _validate_evolve_mode(evolve, budget, max_cycles, yes)

    # Main orchestration flow - call run's callback directly with mapped params
    from bernstein.cli.run_cmd import run

    assert run.callback is not None
    run.callback(
        plan_file=None,
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
        sandbox=sandbox_override,
        allow_paid=allow_paid,
        two_phase_sandbox=False,
        worker_role=None,
        plan_only=plan_only,
        from_plan=Path(from_plan) if from_plan else None,
        auto_approve=auto_approve or yes,
        quiet=False,
        # The group is the interactive form; automation uses `bernstein run`.
        wait=None,
        skip_gate=(),
        skip_gate_reason=None,
        audit=False,
        ab_test=False,
        dry_run=False,
        cprofile=False,
        run_profile=None,
        allow_network=(),
        task_filter=task_filter,
        auto_pr=auto_pr,
        activity_log_path=activity_log_path,
        max_cost_usd=None,
        idle=False,
        budget_spec=None,
        hard_budget_spec=None,
        # Bot-added: drift autofix (regen_contract_drift.py)
        permission_profile=None,
        budget_cap=None,
        retry_budget_spec=None,
        # Bot-added: drift autofix (regen_contract_drift.py)
        criterion_profile=None,
        max_blast_radius=None,
        # Bot-added: drift autofix (regen_contract_drift.py)
        attach=(),
        # Bot-added: drift autofix (regen_contract_drift.py)
        refresh_cache=False,
        force_fresh=force_fresh,
    )


# ---------------------------------------------------------------------------
# Contextual tips
# ---------------------------------------------------------------------------

#: Opt-out switch for the post-command tip line.
TIPS_OPT_OUT_ENV_VAR = "BERNSTEIN_NO_TIPS"


def tips_enabled(ctx: click.Context) -> bool:
    """Return True when a contextual tip may be printed for this invocation.

    Tips are suppressed for machine-readable output (``--json`` / ``--output
    json``), for ``--quiet``, when stdout is not a TTY (pipes, CI logs), when
    the operator sets ``BERNSTEIN_NO_TIPS``, and outside an initialised
    workspace (no ``.sdd/`` to hold the cooldown marker).
    """
    import os

    if os.environ.get(TIPS_OPT_OUT_ENV_VAR):
        return False
    obj = ctx.obj or {}
    if obj.get("JSON") or obj.get("QUIET") or obj.get("HEADLESS"):
        return False
    if not sys.stdout.isatty():
        return False
    return Path(".sdd").is_dir()


@cli.result_callback()
@click.pass_context
def _emit_contextual_tip(ctx: click.Context, _result: object, **_params: object) -> None:
    """Print at most one contextual tip after a subcommand finishes."""
    command = ctx.invoked_subcommand
    if not command or not tips_enabled(ctx):
        return
    try:
        from bernstein.cli.utils.tip_integration import get_tip_for_command, mark_tip_shown, should_show_tip

        if not should_show_tip():
            return
        tip = get_tip_for_command(command)
        if not tip:
            return
        console.print(f"[dim]{tip}[/dim]")
        mark_tip_shown()
    except Exception:  # pragma: no cover - a tip must never break a command
        return


# ---------------------------------------------------------------------------
# Register commands and groups with main CLI
# ---------------------------------------------------------------------------

# From task_cmd module - all registered with @click.command()
_commands.defer("cancel", "bernstein.cli.task_cmd", "cancel")
_commands.defer("task", "bernstein.cli.task_cmd", "task_group")
_commands.defer("add-task", "bernstein.cli.task_cmd", "add_task")
_commands.defer("sync", "bernstein.cli.task_cmd", "sync")
_commands.defer("review", "bernstein.cli.task_cmd", "review_cmd")
_commands.defer("approve", "bernstein.cli.task_cmd", "approve")
_commands.defer("reject", "bernstein.cli.task_cmd", "reject")
_commands.defer("pending", "bernstein.cli.task_cmd", "pending")
_commands.defer("plan", "bernstein.cli.task_cmd", "plan")
_commands.defer("tasks", "bernstein.cli.task_cmd", "plan")
_commands.defer("spec", "bernstein.cli.commands.spec_cmd", "spec_group")
_commands.defer("backlog", "bernstein.cli.task_cmd", "backlog_group")
_commands.defer("bench", "bernstein.eval.bench.bench_cli", "bench_group")
_commands.defer("logs", "bernstein.cli.logs_group_cmd", "logs_group")
_commands.defer("decisions", "bernstein.cli.commands.decisions_cmd", "decisions_group")
_commands.defer("list-tasks", "bernstein.cli.task_cmd", "list_tasks")

# From workspace_cmd module - groups and commands
_commands.defer("workspace", "bernstein.cli.workspace_cmd", "workspace_group")
_commands.defer("config", "bernstein.cli.workspace_cmd", "config_group")

# From advanced_cmd module - groups and commands
_commands.defer("benchmark", "bernstein.cli.eval_benchmark_cmd", "benchmark_alias_group")
_commands.defer("cache", "bernstein.cli.commands.cache_cmd", "cache_group")
_commands.defer("gc", "bernstein.cli.commands.gc_cmd", "gc_group")
_commands.defer("eval", "bernstein.cli.eval_benchmark_cmd", "eval_group")
_commands.defer("best-of-n", "bernstein.cli.commands.best_of_n_rank_cmd", "best_of_n_group")
_commands.defer("dashboard", "bernstein.cli.advanced_cmd", "dashboard")
_commands.defer("live", "bernstein.cli.advanced_cmd", "live")
# Web GUI subcommand. The group itself ships with core (so `bernstein gui --help`
# always works); `bernstein gui serve` performs a runtime check for the [gui]
# extra and exits with a friendly install hint when missing.

_commands.defer("gui", "bernstein.gui.cli", "gui_group", export_name="_gui_group")
_commands.defer("trace", "bernstein.cli.advanced_cmd", "trace_cmd")
_commands.defer("replay", "bernstein.cli.advanced_cmd", "replay_cmd")

_commands.defer("thread", "bernstein.cli.commands.thread_cmd", "thread_cmd", export_name="_thread_cmd")
_commands.defer("github", "bernstein.cli.advanced_cmd", "github_group")
_commands.defer("graph", "bernstein.cli.graph_cmd", "graph_group")
_commands.defer("policy", "bernstein.cli.policy_cmd", "policy_group")
_commands.defer("pool", "bernstein.cli.commands.pool_cmd", "pool_group")
_commands.defer("datasource", "bernstein.cli.commands.datasource_cmd", "datasource_group")
_commands.defer(
    "security",
    "bernstein.cli.commands.role_adapter_policy_cmd",
    "security_group",
    export_name="_role_adapter_security_group",
)
_commands.defer("mcp", "bernstein.cli.advanced_cmd", "mcp_server")

_commands.defer("completions", "bernstein.cli.commands.completions_cmd", "completions_cmd", export_name="completions")
_commands.defer("quarantine", "bernstein.cli.advanced_cmd", "quarantine_group")
_commands.defer("install-hooks", "bernstein.cli.advanced_cmd", "install_hooks")
_commands.defer("plugins", "bernstein.cli.advanced_cmd", "plugins_cmd")
_commands.defer("doctor", "bernstein.cli.advanced_cmd", "doctor")


_commands.defer("recap", "bernstein.cli.advanced_cmd", "recap")
_commands.defer("retro", "bernstein.cli.advanced_cmd", "retro")
_commands.defer("help-all", "bernstein.cli.advanced_cmd", "help_all")
_commands.defer("cleanup", "bernstein.cli.maintenance_cmd", "cleanup_cmd")
_commands.defer("history", "bernstein.cli.maintenance_cmd", "history_cmd")

# Operator-experience commands (feat/operator-experience)
_commands.defer("pr", "bernstein.cli.commands.pr_cmd", "pr_cmd")
_commands.defer("from-ticket", "bernstein.cli.commands.ticket_cmd", "from_ticket")
_commands.defer("ticket", "bernstein.cli.commands.ticket_cmd", "ticket_group")
_commands.defer("remote", "bernstein.cli.commands.remote_cmd", "remote_group")
_commands.defer("hooks", "bernstein.cli.commands.hooks_cmd", "hooks", export_name="hooks_group")
_commands.defer("tunnel", "bernstein.cli.commands.tunnel_cmd", "tunnel_group")
_commands.defer("preview", "bernstein.cli.commands.preview_cmd", "preview_group")
_commands.defer("sandbox", "bernstein.cli.commands.sandbox_cmd", "sandbox_group")
_commands.defer("security-review", "bernstein.cli.commands.security_review_cmd", "security_review_cmd")
_commands.defer("seal", "bernstein.cli.commands.seal_cmd", "seal_group")
_commands.defer("daemon", "bernstein.cli.commands.daemon_cmd", "daemon_group")
_commands.defer("autofix", "bernstein.cli.commands.autofix_cmd", "autofix_group")
_commands.defer("pipeline", "bernstein.cli.commands.pipeline_cmd", "pipeline_group")
_commands.defer("connect", "bernstein.cli.commands.creds_cmd", "connect_cmd")
_commands.defer("creds", "bernstein.cli.commands.creds_cmd", "creds_group")
_commands.defer("criterion-profile", "bernstein.cli.commands.criterion_profile_cmd", "criterion_profile_group")
_commands.defer("review-responder", "bernstein.cli.commands.review_responder_cmd", "review_responder_group")
_commands.defer("issue-to-pr", "bernstein.cli.commands.issue_to_pr_cmd", "issue_to_pr_group")

# Already registered elsewhere
_commands.defer("agents", "bernstein.cli.agents_cmd", "agents_group")
_commands.defer("skills", "bernstein.cli.commands.skills_cmd", "skills_group")


# Skill usage provenance (issue #2301): install receipts + provenance graph.

_commands.defer("skill", "bernstein.cli.commands.skill_cmd", "skill_group", export_name="_skill_group")
# Named team manifests (issue #2248): list/show/drift.
_commands.defer("team", "bernstein.cli.commands.team_cmd", "team_group", export_name="_team_group")
_commands.defer("test", "bernstein.cli.test_cmd", "test_cmd")
# Scoped dashboard tokens (issue #2366): issue/list/revoke under `auth`.

_commands.defer("auth", "bernstein.cli.auth_cmd", "auth_group")
_commands.defer("login", "bernstein.cli.auth_cmd", "auth_login")
_commands.defer("evolve", "bernstein.cli.evolve_cmd", "evolve")
_commands.defer("cost", "bernstein.cli.cost", "cost_cmd")
_commands.defer("cost-envelopes", "bernstein.cli.cost", "cost_envelopes_alias_cmd")
_commands.defer("estimate", "bernstein.cli.cost", "estimate_alias_cmd")
_commands.defer("status", "bernstein.cli.status_cmd", "status")
_commands.defer("ps", "bernstein.cli.status_cmd", "ps_cmd")
_commands.defer("stop", "bernstein.cli.stop_cmd", "stop")
_commands.defer("test-adapter", "bernstein.cli.commands.adapter_cmd", "test_adapter")
_commands.defer("adapters", "bernstein.cli.commands.adapter_cmd", "adapters_group")
_commands.defer("integrations", "bernstein.cli.commands.integrations_cmd", "integrations_group")
_commands.defer("endpoints", "bernstein.cli.commands.endpoints_cmd", "endpoints_group")
_commands.defer("trackers", "bernstein.cli.commands.trackers_cmd", "trackers_group")
_commands.defer("run", "bernstein.cli.run_cmd", "run")
_commands.defer("cook", "bernstein.cli.run_cmd", "cook")
_commands.defer("init", "bernstein.cli.run_cmd", "init")
_commands.defer("start", "bernstein.cli.run_cmd", "start")
_commands.defer("serve", "bernstein.cli.run_cmd", "serve")
_commands.defer("demo", "bernstein.cli.run_cmd", "demo")
_commands.defer("checkpoint", "bernstein.cli.commands.checkpoint_cmd", "checkpoint_cmd")
_commands.defer("resume", "bernstein.cli.commands.resume_cmd", "resume_cmd")
_commands.defer("fork", "bernstein.cli.commands.fork_cmd", "fork_cmd")
_commands.defer("wrap-up", "bernstein.cli.wrap_up_cmd", "wrap_up")
_commands.defer("audit", "bernstein.cli.audit_cmd", "audit_group")
_commands.defer("events", "bernstein.cli.commands.events_cmd", "events_group")
_commands.defer("bom", "bernstein.cli.commands.bom_cmd", "bom_group")
_commands.defer("bundle", "bernstein.cli.commands.bundle_cmd", "bundle_group")
_commands.defer("compliance", "bernstein.cli.commands.compliance_cmd", "compliance_group")
_commands.defer("compaction", "bernstein.cli.commands.compaction_cmd", "compaction_group")
_commands.defer("verify", "bernstein.cli.verify_cmd", "verify_cmd")
_commands.defer("chaos", "bernstein.cli.chaos_cmd", "chaos_group")
_commands.defer("manifest", "bernstein.cli.manifest_cmd", "manifest_group")
_commands.defer("memory", "bernstein.cli.memory_cmd", "memory_group")
_commands.defer("prompts", "bernstein.cli.prompts_cmd", "prompts_group")
_commands.defer("ci", "bernstein.cli.ci_cmd", "ci_group")
_commands.defer("cloud", "bernstein.cli.cloud_cmd", "cloud_group")
_commands.defer("gateway", "bernstein.cli.commands.gateway_cmd", "gateway_group")
_commands.defer("export", "bernstein.cli.commands.export_cmd", "export_cmd")
_commands.defer("report", "bernstein.cli.report_cmd", "report_cmd")
_commands.defer("slo", "bernstein.cli.slo_cmd", "slo_cmd")
_commands.defer("man-pages", "bernstein.cli.man_page", "man_pages_cmd")
_commands.defer("workflow", "bernstein.cli.workflow_cmd", "workflow_group")
_commands.defer("recipes", "bernstein.cli.recipes_cmd", "recipes_group")
_commands.defer("knowledge", "bernstein.cli.commands.knowledge_cmd", "knowledge_group")
_commands.defer("quickstart", "bernstein.cli.quickstart_cmd", "quickstart_alias_cmd")
_commands.defer("scaffold", "bernstein.cli.scaffold_cmd", "scaffold_cmd")
_commands.defer("watch", "bernstein.cli.watch_cmd", "watch_cmd")
_commands.defer("wiki", "bernstein.cli.wiki_cmd", "wiki_group")
_commands.defer("listen", "bernstein.cli.voice_cmd", "listen_cmd")
_commands.defer("self-update", "bernstein.cli.commands.self_update_cmd", "self_update_cmd")

_commands.defer("self", "bernstein.cli.commands.self_update_cmd", "self_group")
_commands.defer("scenario", "bernstein.cli.commands.scenario_cmd", "scenario_group")

_commands.defer("undo", "bernstein.cli.undo_cmd", "undo_cmd")
# `bernstein trust`: the one sanctioned way to write the workspace trust
# record that script hooks, plugin hooks and workflow command nodes require.

_commands.defer("trust", "bernstein.cli.commands.trust_cmd", "trust_cmd")
_commands.defer("worker", "bernstein.cli.commands.worker_cmd", "worker")
_commands.defer("worktrees", "bernstein.cli.worktrees_cmd", "worktrees_group")
_commands.defer("diff", "bernstein.cli.diff_cmd", "diff_cmd")
_commands.defer("merge", "bernstein.cli.merge_cmd", "merge_cmd")
_commands.defer("migrate", "bernstein.cli.migrate_cmd", "migrate_cmd")
# #3142: ``bernstein changelog`` is now a group (default = run-changelog
# behaviour). ``bernstein run-changelog`` stays as a top-level deprecated
# alias — removed in a release after the 4.0 line that introduced it — so
# existing scripts print a notice on stderr rather than silently changing
# meaning at the bare name.
_commands.defer("changelog", "bernstein.cli.changelog_cmd", "changelog_group", export_name="changelog_cmd")
_commands.defer("run-changelog", "bernstein.cli.changelog_cmd", "changelog_run_alias")
_commands.defer("run-lookup", "bernstein.cli.commands.run_names_cmd", "run_lookup_cmd")
_commands.defer("dr", "bernstein.cli.disaster_recovery_cmd", "dr_group")
_commands.defer("profile", "bernstein.cli.profile_cmd", "profile_cmd")
_commands.defer("templates", "bernstein.cli.templates_cmd", "templates_group")
_commands.defer("validate", "bernstein.cli.plan_validate_cmd", "validate_alias_cmd")
_commands.defer("impact", "bernstein.cli.commands.impact_cmd", "impact_group")
_commands.defer("dep-impact", "bernstein.cli.commands.impact_cmd", "dep_impact_alias_cmd")
_commands.defer("fingerprint", "bernstein.cli.fingerprint_cmd", "fingerprint_group")
_commands.defer("fleet", "bernstein.cli.commands.fleet_cmd", "fleet_group")
_commands.defer("triggers", "bernstein.cli.triggers_cmd", "triggers_group")
_commands.defer("schedule", "bernstein.cli.commands.schedule_cmd", "schedule_group")

# Per-goal SLA contracts + signed violation receipts (#2549)

_commands.defer("sla", "bernstein.cli.commands.sla_cmd", "sla_group")

# Operator supervisor surface (#1800)

_commands.defer("supervisor", "bernstein.cli.commands.supervisor_cmd", "supervisor_group")

# Citation/reference existence verifier (closes #1402)
_commands.defer("quality", "bernstein.cli.commands.citation_cmd", "quality_group", export_name="citation_quality_group")

# New CLI commands
_commands.defer("dry-run", "bernstein.cli.dry_run_cmd", "dry_run_cmd")
_commands.defer("explain", "bernstein.cli.explain_help_cmd", "explain_help_cmd")
_commands.defer("config-path", "bernstein.cli.config_path_cmd", "config_path_cmd")
_commands.defer("desktop-register", "bernstein.cli.commands.desktop_register_cmd", "desktop_register_cmd")
_commands.defer("init-wizard", "bernstein.cli.init_wizard_cmd", "init_wizard_alias_cmd")
_commands.defer("aliases", "bernstein.cli.aliases", "aliases_cmd")
_commands.defer("debug-bundle", "bernstein.cli.debug_cmd", "debug_cmd")

# op-002: interactive tool-call approval (approve-tool / reject-tool).
# These are the tool-call resolvers; task-level ``approve``/``reject`` live in
# approve_cmd/reject_cmd and also accept ``--tool <id>`` for this queue.
_commands.defer("approve-tool", "bernstein.cli.commands.approval_cmd", "approve_tool_cmd")
_commands.defer("reject-tool", "bernstein.cli.commands.approval_cmd", "reject_tool_cmd")
# ``bernstein debug`` is the structured diagnostics group; ``debug bundle``
# exports a redacted ZIP for bug reports. The flat ``debug-bundle`` alias
# above stays for back-compat with older issue templates.
_commands.defer("debug", "bernstein.cli.debug_bundle", "debug_group")

# Chat-control bridges (op-001)

_commands.defer("chat", "bernstein.cli.commands.chat_cmd", "chat_group")

# release/1.9: native Agent Client Protocol (ACP) bridge for IDE clients.

_commands.defer("acp", "bernstein.cli.commands.acp_cmd", "acp_group")

# Cross-organisation A2A interop: signed capability cards + lineage interop.

_commands.defer("interop", "bernstein.cli.commands.interop_cmd", "interop_group")

# A2A node surface: publish this node to agent registries, verify the
# lineage receipts it returns on inbound tasks.

_commands.defer("a2a", "bernstein.cli.commands.a2a_cmd", "a2a_group")

# release/1.9: outbound notification drivers (Telegram, Slack, Discord, Email, Webhook, Shell).

_commands.defer("notify", "bernstein.cli.commands.notify_cmd", "notify_group")

# Short-lived-token broker: per-task credentials minted from a backing store.

_commands.defer("secrets", "bernstein.cli.commands.secrets_cmd", "secrets_group")

# Per-artifact lineage trail (output → producer + inputs).

_commands.defer("lineage", "bernstein.cli.commands.lineage_cmd", "lineage_cmd")

# C2PA content credentials projected from the lineage spine (#2303).

_commands.defer("credential", "bernstein.cli.commands.credential_cmd", "credential_group")

# Verifiable spending mandates as journal-anchored consent receipts (#2306).

_commands.defer("mandate", "bernstein.cli.commands.mandate_cmd", "mandate_group")

# Signed payment mandates + chain-anchored transaction receipts (#2612).

_commands.defer("payment-mandate", "bernstein.cli.commands.payment_mandate_cmd", "payment_mandate_group")

# Attested pull-request review receipts linking issue -> plan -> tool calls -> diff (#2296).

_commands.defer("review-receipt", "bernstein.cli.commands.review_receipt_cmd", "review_receipt_group")

# Content-addressed anchors for operator review annotations (#3456).

_commands.defer("review-annotation", "bernstein.cli.commands.review_annotation_cmd", "review_annotation_group")
# Journal-anchored stall escalation receipts (#2299).

_commands.defer("escalation", "bernstein.cli.commands.escalation_cmd", "escalation_group")
# Intent capsules with deterministic drift verification (#2514).

_commands.defer("intent", "bernstein.cli.commands.intent_cmd", "intent_group")
# Chain-anchored worker context capsules (#2545).

_commands.defer("context", "bernstein.cli.commands.context_cmd", "context_group")
# Fleet config plane: audit-chained variables, named connection documents,
# and switchable operating contexts (#2550). The ``context`` name is taken
# by the worker context-capsule surface above, so operating contexts are
# exposed as ``ctx``.

_commands.defer("var", "bernstein.cli.commands.var_cmd", "var_group")
_commands.defer("conn", "bernstein.cli.commands.conn_cmd", "conn_group")
_commands.defer("ctx", "bernstein.cli.commands.ctx_cmd", "ctx_group")
# Signed maker-checker / judge-panel gate adjudications (#2294).

_commands.defer("gate", "bernstein.cli.commands.gate_cmd", "gate_group")

# RBAC + budget decisions as verifiable projections over the audit chain (#2309).

_commands.defer("govern", "bernstein.cli.commands.governance_cmd", "govern_group")

# Deprecated governance alias for backward compatibility.

_commands.defer("governance", "bernstein.cli.commands.governance_cmd", "governance_alias_cmd")

# Per-tool-call snapshots + stacked agent branches.

_commands.defer("git", "bernstein.cli.commands.git_cmd", "git_cmd")

# Canonical AGENTS.md generator + cross-CLI rewrite (cursor / claude / aider / goose).

_commands.defer("agents-md", "bernstein.cli.commands.agents_md_cmd", "agents_md_cmd")

# Translated README drift gate (issue #3425): verify / sync bindings.

_commands.defer("readme-l10n", "bernstein.cli.commands.readme_l10n_cmd", "readme_l10n_cmd")

# Air-gap distribution: build / verify wheel bundle.

_commands.defer("wheelhouse", "bernstein.cli.commands.wheelhouse_cmd", "wheelhouse_group")

# rt-003: Claude Code Routine <-> scenario bridge.

_commands.defer("routine", "bernstein.cli.commands.routine_cmd", "routine_alias_group")

# Cluster lifecycle helpers (mTLS bootstrap, etc.)

_commands.defer("cluster", "bernstein.cli.commands.cluster_cmd", "cluster_group")

# op-005: cross-surface session handoff (terminal <-> chat <-> dashboard).

_commands.defer("handoff", "bernstein.cli.commands.handoff_cmd", "handoff_group")

# Install-rev fingerprint operator helpers.

_commands.defer("identity", "bernstein.cli.commands.identity_cmd", "identity_group")

# SPIFFE-compatible workload identity helpers (issue #2363).

_commands.defer("spiffe", "bernstein.cli.commands.spiffe_cmd", "spiffe_group")

# Delegation-receipt verification for the principal->orchestrator->sub-agent
# chain (issue #2305).

_commands.defer("delegation", "bernstein.cli.commands.delegation_cmd", "delegation_group")
_commands.defer("analyze", "bernstein.cli.commands.analyze_cmd", "analyze_cmd")

# Blast-radius scorer (issue #1322): inspect + ad-hoc score a change.
_commands.defer("blast-radius", "bernstein.cli.commands.impact_cmd", "blast_radius_alias_group")

# Recorded run-session inspection + fork (#1222).

_commands.defer("session", "bernstein.cli.commands.session_cmd", "session_group")

# Side-by-side adapter comparison (feat-cli-comparison-mode).

_commands.defer("compare", "bernstein.cli.commands.compare_cmd", "compare_cmd")

# Agent abandonment ledger (#1350).

_commands.defer("abandonments", "bernstein.cli.commands.abandonments_cmd", "abandonments_group")

# Digital-twin orchestration simulator (#1374).

_commands.defer("simulate", "bernstein.cli.commands.simulate_cmd", "simulate_cmd")

# Opt-in operator observability (closes spec 2026-05-17).
# Default off.  Precedence: DO_NOT_TRACK > BERNSTEIN_TELEMETRY > file > off.
_commands.defer("telemetry", "bernstein.cli.commands.telemetry_cmd", "telemetry_group")

# Scheduled upstream-signal sweep (operator-reviewable rollup, no auto-filing).

_commands.defer("trend-scan", "bernstein.cli.commands.trend_scan_cmd", "trend_scan_group")

# Audited webhook-node receipts: signed inbound + outbound webhook events (#2310).

_commands.defer("webhook", "bernstein.cli.commands.webhook_cmd", "webhook_group")

# Typed activity boundary: verify any-modality activity crossings from the journal (#2311).

_commands.defer("activity", "bernstein.cli.commands.activity_cmd", "activity_group")

# Content-addressed verification evidence bundles: signed proof-of-done per task (#2362).

_commands.defer("evidence", "bernstein.cli.commands.evidence_cmd", "evidence_group")

# Agent-posted, journal-anchored task artifacts: reports, tables, links (#2553).

_commands.defer("artifacts", "bernstein.cli.commands.artifacts_cmd", "artifacts_group")

# Verify a non-coding task's signed, content-addressed artifact (#2608).

_commands.defer("artifact", "bernstein.cli.commands.artifact_cmd", "artifact_group")

# Executor admission policy: which adapters, models, endpoints and sandbox
# tiers this repository may spawn on, checked without running anything (#4907).

_commands.defer("admission", "bernstein.cli.commands.admission_cmd", "admission_group")

# In-process verification gate driven by worker hooks: blocks a failing
# completion or an out-of-scope write in-session, sealing gate receipts (#2360).

_commands.defer("hook-gate", "bernstein.cli.commands.hook_gate_cmd", "hook_gate_group")

# Durable work ledger: resumable task-graph state anchored to a git ref (#2358).

_commands.defer("ledger", "bernstein.cli.commands.ledger_cmd", "ledger_group")

# Finished-run classification projected from the work ledger: which runs
# opened a PR, which failed the gate, which were killed mid-flight (#4465).

_commands.defer("runs", "bernstein.cli.commands.runs_cmd", "runs_group")

# Ledger-projected missions: multi-day goals with phase gates + envelopes (#2509).

_commands.defer("mission", "bernstein.cli.commands.mission_cmd", "mission_group")
# Named resource pools with lease-backed admission projected from the ledger (#2544).

_commands.defer("limits", "bernstein.cli.commands.limits_cmd", "limits_group")

# Detached run service: submit a goal, disconnect, reattach later (#2352).

_commands.defer("run-service", "bernstein.cli.commands.run_service_cmd", "run_service_group")

# Tournament runs: parallel attempts selected by deterministic evaluators (#2353).

_commands.defer("tournament", "bernstein.cli.commands.tournament_cmd", "tournament_group")

# Documented-but-unregistered commands (#3139). Both modules shipped complete,
# were listed in the lazy-import map, and were documented in the CLI reference,
# but no ``add_command`` call ever made them reachable. ``dep-impact``'s own
# help text cross-references ``api-check``, so following that pointer produced
# "No such command" until this registration landed.

_commands.defer("api-check", "bernstein.cli.commands.api_check_cmd", "api_check_cmd")
_commands.defer("ab-test", "bernstein.cli.commands.ab_test_cmd", "ab_test_cmd")
_commands.defer("receipt", "bernstein.cli.commands.receipt_cmd", "receipt_group")
_commands.defer("model", "bernstein.cli.commands.model_cmd", "model_group")
_commands.defer("volunteer", "bernstein.cli.commands.volunteer_cmd", "volunteer_group")

# ``bernstein adopt`` (#5435, slice 1): detect the running agent session to
# bring under governance. Only ``--dry-run`` is implemented; it writes nothing.

_commands.defer("adopt", "bernstein.cli.commands.adopt_cmd", "adopt_cmd")

# Same defect as #3139 above: the skeleton was documented in
# src/bernstein/cli/AGENTS.md, but no ``add_command`` call ever made it
# reachable (#5880).

_commands.defer("insights", "bernstein.cli.commands.insights_cmd", "insights")
