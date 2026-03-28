"""Workspace and configuration commands for Bernstein CLI.

This module contains all workspace-related commands and groups:
  workspace_group (clone, validate)
  config_group (set, get, list)
  plan

All commands are registered with the main CLI group in main.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import (
    console,
    find_seed_file,
    server_get,
)


# Create standalone groups that will be registered with main CLI in main.py
@click.group("workspace", invoke_without_command=True)
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


# Config group with subcommands
@click.group("config")
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


@click.command("plan")
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
    import json
    from typing import cast

    from rich.table import Table

    from bernstein.cli.helpers import STATUS_COLORS, server_get

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
