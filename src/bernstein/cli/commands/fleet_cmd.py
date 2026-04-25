"""``bernstein fleet`` — supervisory dashboard across multiple projects.

This is the CLI entry point. The actual aggregator and rendering live in
:mod:`bernstein.core.fleet`; this module only wires Click subcommands to
those primitives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from bernstein.core.fleet import (
    FleetAggregator,
    FleetConfig,
    bulk_cost_report,
    bulk_pause,
    bulk_resume,
    bulk_stop,
    default_projects_config_path,
    load_projects_config,
    select_projects,
)

logger = logging.getLogger(__name__)
_console = Console()


def _resolve_config(path: str | None) -> FleetConfig:
    target = Path(path).expanduser() if path else None
    return load_projects_config(target)


def _print_config_errors(config: FleetConfig) -> None:
    if not config.errors:
        return
    for err in config.errors:
        tag = "global" if err.index < 0 else f"project[{err.index}]"
        _console.print(f"[yellow]config {tag}:[/yellow] {err.message}")


@click.group("fleet", invoke_without_command=True)
@click.option(
    "--config",
    "config_path",
    default=None,
    help=f"Path to fleet config (default: {default_projects_config_path()}).",
)
@click.option(
    "--web",
    "web_bind",
    default=None,
    help="Run the web view instead of the TUI. Bind format: ``[host:]port``.",
)
@click.pass_context
def fleet_group(
    ctx: click.Context,
    config_path: str | None,
    web_bind: str | None,
) -> None:
    """Supervisory dashboard for multiple Bernstein projects."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    if ctx.invoked_subcommand is None:
        config = _resolve_config(config_path)
        if web_bind is not None:
            _run_web(config, web_bind)
            return
        _run_tui(config)


def _run_tui(config: FleetConfig) -> None:
    if not config.projects:
        _print_config_errors(config)
        _console.print(
            "[red]No projects configured.[/red] "
            f"Edit {default_projects_config_path()} and add [[project]] blocks."
        )
        sys.exit(2)

    async def _main() -> None:
        aggregator = FleetAggregator(config.projects)
        await aggregator.start()
        try:
            try:
                from bernstein.core.fleet.tui import build_textual_app
            except ImportError:
                _fallback_table_render(aggregator, config)
                return
            app = build_textual_app(aggregator, config)
            await app.run_async()
        finally:
            await aggregator.stop()

    asyncio.run(_main())


def _fallback_table_render(aggregator: FleetAggregator, config: FleetConfig) -> None:
    """Fallback Rich-based renderer when Textual is unavailable."""
    from bernstein.core.fleet.tui import build_rows, format_footer

    rows, total = build_rows(aggregator)
    table = Table(title="Bernstein fleet")
    for col in [
        "Project",
        "State",
        "Run",
        "Agents",
        "Approvals",
        "Last SHA",
        "Cost (7d)",
        "Sparkline",
        "Chain",
    ]:
        table.add_column(col)
    for row in rows:
        table.add_row(
            row.name,
            row.state,
            row.run_state,
            str(row.agents),
            str(row.approvals),
            row.last_sha,
            f"${row.cost_usd:.2f}",
            row.sparkline,
            "ok" if row.chain_ok else "BROKEN",
        )
    _console.print(table)
    _console.print(format_footer(config, rows, total))


def _parse_bind(bind: str) -> tuple[str, int]:
    text = bind.strip()
    if text.startswith(":"):
        return "127.0.0.1", int(text[1:])
    if ":" in text:
        host, port = text.rsplit(":", 1)
        return host or "127.0.0.1", int(port)
    return "127.0.0.1", int(text)


def _run_web(config: FleetConfig, bind: str) -> None:
    if not config.projects:
        _print_config_errors(config)
        _console.print(
            "[red]No projects configured.[/red] "
            "Add [[project]] blocks before launching --web."
        )
        sys.exit(2)
    host, port = _parse_bind(bind)

    try:
        import uvicorn
    except ImportError:
        _console.print("[red]uvicorn is required for fleet --web.[/red]")
        sys.exit(2)

    from bernstein.core.fleet.web import build_fleet_app

    async def _bootstrap() -> tuple[FleetAggregator, Any]:
        aggregator = FleetAggregator(config.projects)
        await aggregator.start()
        return aggregator, build_fleet_app(aggregator, config)

    aggregator, app = asyncio.run(_bootstrap())
    _console.print(f"[green]Bernstein fleet web[/green] listening on http://{host}:{port}")
    _print_config_errors(config)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        asyncio.run(aggregator.stop())


# ---------------------------------------------------------------------------
# Bulk subcommands
# ---------------------------------------------------------------------------


def _bulk_target(
    config: FleetConfig,
    names: tuple[str, ...] | None,
    filter_expression: str | None,
) -> list[Any]:
    """Resolve a target list using the *static* config snapshot.

    Unlike the TUI, the CLI bulk path doesn't need the live aggregator
    — projects are filtered by their on-disk cost history when a filter
    references ``cost``.
    """
    from bernstein.core.fleet.aggregator import ProjectSnapshot, ProjectState
    from bernstein.core.fleet.cost_rollup import rollup_costs

    rollup = rollup_costs(
        {p.name: p.sdd_dir for p in config.projects}, window_days=7
    )
    snapshots = [
        ProjectSnapshot(
            name=p.name,
            state=ProjectState.ONLINE,
            cost_usd=float(rollup.per_project.get(p.name, {}).get("total_usd") or 0.0),
        )
        for p in config.projects
    ]
    return select_projects(
        config.projects,
        snapshots,
        names=list(names) if names else None,
        filter_expression=filter_expression,
    )


def _print_bulk_result(result: Any) -> None:
    payload: dict[str, Any] = {
        "action": result.action,
        "succeeded": list(result.succeeded),
        "failed": dict(result.failed),
    }
    _console.print_json(json.dumps(payload))


@fleet_group.command("bulk-stop")
@click.option("--names", multiple=True, help="Restrict to listed project names.")
@click.option("--filter", "filter_expression", default=None, help="Filter expression e.g. cost>5.")
@click.pass_context
def bulk_stop_cmd(
    ctx: click.Context,
    names: tuple[str, ...],
    filter_expression: str | None,
) -> None:
    """Stop every matching project via its CLI."""
    config = _resolve_config(ctx.obj.get("config_path"))
    targets = _bulk_target(config, names, filter_expression)
    result = asyncio.run(bulk_stop(targets))
    _print_bulk_result(result)


@fleet_group.command("bulk-pause")
@click.option("--names", multiple=True, help="Restrict to listed project names.")
@click.option("--filter", "filter_expression", default=None, help="Filter expression.")
@click.pass_context
def bulk_pause_cmd(
    ctx: click.Context,
    names: tuple[str, ...],
    filter_expression: str | None,
) -> None:
    """Pause every matching project (stops its daemon)."""
    config = _resolve_config(ctx.obj.get("config_path"))
    targets = _bulk_target(config, names, filter_expression)
    result = asyncio.run(bulk_pause(targets))
    _print_bulk_result(result)


@fleet_group.command("bulk-resume")
@click.option("--names", multiple=True, help="Restrict to listed project names.")
@click.option("--filter", "filter_expression", default=None, help="Filter expression.")
@click.pass_context
def bulk_resume_cmd(
    ctx: click.Context,
    names: tuple[str, ...],
    filter_expression: str | None,
) -> None:
    """Resume every matching project (restarts its daemon)."""
    config = _resolve_config(ctx.obj.get("config_path"))
    targets = _bulk_target(config, names, filter_expression)
    result = asyncio.run(bulk_resume(targets))
    _print_bulk_result(result)


@fleet_group.command("bulk-cost-report")
@click.option("--names", multiple=True, help="Restrict to listed project names.")
@click.option("--filter", "filter_expression", default=None, help="Filter expression.")
@click.pass_context
def bulk_cost_report_cmd(
    ctx: click.Context,
    names: tuple[str, ...],
    filter_expression: str | None,
) -> None:
    """Run ``bernstein cost report`` against every matching project."""
    config = _resolve_config(ctx.obj.get("config_path"))
    targets = _bulk_target(config, names, filter_expression)
    result = asyncio.run(bulk_cost_report(targets))
    _print_bulk_result(result)


@fleet_group.command("ls")
@click.pass_context
def ls_cmd(ctx: click.Context) -> None:
    """List configured projects without launching the dashboard."""
    config = _resolve_config(ctx.obj.get("config_path"))
    table = Table(title="Bernstein fleet — configured projects")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Task server")
    for project in config.projects:
        table.add_row(project.name, str(project.path), project.task_server_url)
    _console.print(table)
    _print_config_errors(config)
