"""``bernstein explain <command>`` -- detailed help with examples.

CLI-008: Provide extended help for any Bernstein command, including
usage examples, common patterns, and related commands.
"""

from __future__ import annotations

from typing import Any

import click

from bernstein.cli.helpers import console

# ---------------------------------------------------------------------------
# Command help database
# ---------------------------------------------------------------------------

_COMMAND_EXAMPLES: dict[str, dict[str, Any]] = {
    "run": {
        "summary": "Start orchestrating agents for a project.",
        "examples": [
            ("bernstein run", "Run from bernstein.yaml or backlog"),
            ("bernstein run plan.yaml", "Execute a specific plan file"),
            ('bernstein -g "Add JWT auth"', "Run with an inline goal"),
            ("bernstein run --dry-run", "Preview without executing"),
            ("bernstein run --plan-only", "Show plan without running"),
        ],
        "related": ["init", "stop", "status", "plan"],
        "tips": [
            "Use --budget to set a cost cap in USD.",
            "Use --auto-approve to skip the confirmation prompt.",
            "Use --cli claude to force a specific agent.",
        ],
    },
    "init": {
        "summary": "Initialize a Bernstein workspace in the current directory.",
        "examples": [
            ("bernstein init", "Create .sdd/ and bernstein.yaml"),
            ("bernstein init --dir /path/to/project", "Init in a specific directory"),
        ],
        "related": ["run", "doctor", "config"],
        "tips": [
            "This creates the .sdd/ directory structure and a default bernstein.yaml.",
            "Run 'bernstein doctor' after init to verify the setup.",
        ],
    },
    "stop": {
        "summary": "Stop running agents gracefully or forcefully.",
        "examples": [
            ("bernstein stop", "Graceful stop (agents save work)"),
            ("bernstein stop --force", "Kill all agents immediately"),
        ],
        "related": ["run", "status", "ps"],
        "tips": [
            "Graceful stop lets agents finish current work and save state.",
            "Use --force only when agents are stuck or unresponsive.",
        ],
    },
    "status": {
        "summary": "Show task summary and agent health.",
        "examples": [
            ("bernstein status", "Rich status display"),
            ("bernstein status --json", "Machine-readable JSON output"),
        ],
        "related": ["ps", "live", "dashboard"],
        "tips": [
            "Use 'bernstein live' for a real-time TUI dashboard.",
            "Use 'bernstein ps' to see running agent processes.",
        ],
    },
    "doctor": {
        "summary": "Run self-diagnostics to check the Bernstein installation.",
        "examples": [
            ("bernstein doctor", "Run all health checks"),
            ("bernstein doctor --json", "Machine-readable JSON output"),
            ("bernstein doctor --fix", "Attempt to auto-fix issues"),
        ],
        "related": ["init", "status"],
        "tips": [
            "Run doctor before your first 'bernstein run' to catch issues early.",
            "The --fix flag can resolve common issues like stale PID files.",
        ],
    },
    "live": {
        "summary": "Launch the interactive TUI dashboard.",
        "examples": [
            ("bernstein live", "Start the 3-column TUI"),
            ("bernstein live --classic", "Use the simpler Rich Live display"),
            ("bernstein live --interval 5", "Set polling interval to 5 seconds"),
        ],
        "related": ["dashboard", "status", "ps"],
        "tips": [
            "Press Ctrl+C to exit the TUI.",
            "The TUI shows agents, tasks, and activity in real-time.",
        ],
    },
    "cost": {
        "summary": "Show spend breakdown by model and task.",
        "examples": [
            ("bernstein cost", "Show current run costs"),
            ("bernstein cost --json", "JSON format for scripting"),
        ],
        "related": ["status", "recap"],
        "tips": [
            "Use --budget on 'bernstein run' to set a cost cap.",
        ],
    },
    "replay": {
        "summary": "Replay events from a previous orchestration run.",
        "examples": [
            ("bernstein replay list", "List available runs"),
            ("bernstein replay latest", "Replay most recent run"),
            ("bernstein replay 20240315-143022", "Replay a specific run"),
            ("bernstein replay latest --as-json", "Raw JSONL output"),
            ("bernstein replay latest --limit 10", "Show first 10 events"),
        ],
        "related": ["trace", "recap", "retro"],
        "tips": [
            "Replays include deterministic fingerprints for audit trails.",
            "Use --limit to focus on specific event windows.",
        ],
    },
    "diff": {
        "summary": "Show the git diff of what an agent changed.",
        "examples": [
            ("bernstein diff 90307ac2", "Show diff for a task"),
            ("bernstein diff 90307ac2 --stat", "Summary only"),
            ("bernstein diff --compare task1 task2", "Side-by-side comparison"),
            ("bernstein diff 90307ac2 --raw", "Raw diff without highlighting"),
        ],
        "related": ["trace", "replay", "review"],
        "tips": [
            "Diffs work from live worktrees, branches, or merged commits.",
            "Use --compare for A/B testing different agent approaches.",
        ],
    },
    "explain": {
        "summary": "Get detailed help and examples for any bernstein command.",
        "examples": [
            ("bernstein explain run", "Detailed help for 'bernstein run'"),
            ("bernstein explain doctor", "Detailed help for 'bernstein doctor'"),
        ],
        "related": ["help-all"],
        "tips": [
            "Use 'bernstein --help' for the quick reference.",
            "Use 'bernstein help-all' for the full command listing.",
        ],
    },
    "completions": {
        "summary": "Generate shell completion scripts for bash, zsh, or fish.",
        "examples": [
            ('eval "$(bernstein completions --shell bash)"', "Add to ~/.bashrc"),
            ('eval "$(bernstein completions --shell zsh)"', "Add to ~/.zshrc"),
            ("bernstein completions --shell fish | source", "Add to fish config"),
        ],
        "related": ["help-all", "explain"],
        "tips": [
            "Completions cover all subcommands and options.",
            "Re-run after upgrading Bernstein to get new command completions.",
        ],
    },
}


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("explain")
@click.argument("command_name", required=False, default=None)
def explain_help_cmd(command_name: str | None) -> None:
    """Show detailed help with examples for a command.

    \b
    Provides extended usage information, examples, related commands,
    and tips for any Bernstein CLI command.

    \b
    Examples:
      bernstein explain run
      bernstein explain doctor
      bernstein explain replay
    """
    from rich.panel import Panel
    from rich.table import Table

    if command_name is None:
        # List all commands with examples
        console.print("\n[bold]Available commands with detailed help:[/bold]\n")
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Command", style="green", width=16)
        table.add_column("Summary")
        for cmd, info in sorted(_COMMAND_EXAMPLES.items()):
            table.add_row(cmd, info["summary"])
        console.print(table)
        console.print("\n[dim]Usage: bernstein explain <command>[/dim]")
        return

    info = _COMMAND_EXAMPLES.get(command_name)
    if info is None:
        console.print(f"[yellow]No detailed help for:[/yellow] {command_name}")
        console.print("[dim]Try 'bernstein explain' to see available commands.[/dim]")
        # Still show the standard --help if the command exists
        console.print(f"[dim]Or try: bernstein {command_name} --help[/dim]")
        return

    # Summary
    console.print(f"\n[bold cyan]bernstein {command_name}[/bold cyan]")
    console.print(f"  {info['summary']}\n")

    # Examples
    examples_table = Table(show_header=True, header_style="bold green", expand=True)
    examples_table.add_column("Example", style="white", no_wrap=True, ratio=2)
    examples_table.add_column("Description", style="dim", ratio=1)
    for cmd, desc in info.get("examples", []):
        examples_table.add_row(f"  $ {cmd}", desc)
    console.print(Panel(examples_table, title="[bold]Examples[/bold]", border_style="green"))

    # Tips
    tips = info.get("tips", [])
    if tips:
        tips_text = "\n".join(f"  [dim]-[/dim] {tip}" for tip in tips)
        console.print(Panel(tips_text, title="[bold]Tips[/bold]", border_style="yellow"))

    # Related commands
    related = info.get("related", [])
    if related:
        related_str = "  ".join(f"[cyan]bernstein {r}[/cyan]" for r in related)
        console.print(f"\n[bold]Related:[/bold] {related_str}")
    console.print()
