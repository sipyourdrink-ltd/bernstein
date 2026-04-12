"""CLI group for Bernstein MCP server and marketplace management."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import SERVER_URL, console
from bernstein.core.mcp_marketplace import marketplace_entries, marketplace_entry
from bernstein.core.mcp_registry import load_catalog_entries, upsert_catalog_entry
from bernstein.core.mcp_usage_analytics import (
    MCPUsageAnalyticsReport,
    analyze_mcp_usage,
    write_tool_inventory_snapshot,
)


def _catalog_path() -> Path:
    """Return the project-local MCP catalog path."""
    return Path.cwd() / ".sdd" / "config" / "mcp_servers.yaml"


@click.group("mcp", invoke_without_command=True)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    show_default=True,
    help="Transport mechanism.",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Host for HTTP transport.")
@click.option("--port", type=int, default=8053, show_default=True, help="Port for HTTP transport.")
@click.option(
    "--server-url",
    default=SERVER_URL,
    show_default=True,
    help="Bernstein server URL.",
)
@click.pass_context
def mcp_server(ctx: click.Context, transport: str, host: str, port: int, server_url: str) -> None:
    """Run the MCP server or manage the bundled MCP marketplace."""
    if ctx.invoked_subcommand is not None:
        return

    from bernstein.mcp.server import run_sse, run_stdio

    if transport == "stdio":
        run_stdio(server_url=server_url)
    else:
        console.print(f"[cyan]MCP Server[/cyan] starting on SSE ({host}:{port})")
        console.print(f"[dim]Backend: {server_url}[/dim]")
        run_sse(server_url=server_url, host=host, port=port)


@mcp_server.command("list")
def list_marketplace() -> None:
    """List bundled MCP marketplace entries and install status."""
    from rich.table import Table

    installed = {entry.name for entry in load_catalog_entries(_catalog_path()) if entry.name}
    table = Table(title="MCP Marketplace", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Package")
    table.add_column("Capabilities")
    table.add_column("Env")
    table.add_column("Status")

    for entry in marketplace_entries():
        table.add_row(
            entry.name,
            entry.package,
            ", ".join(entry.capabilities) or "--",
            ", ".join(entry.env_required) or "--",
            "installed" if entry.name in installed else "available",
        )

    console.print(table)


@mcp_server.command("install")
@click.argument("server_name")
def install_marketplace_entry(server_name: str) -> None:
    """Install or update a bundled MCP server entry in `.sdd/config/mcp_servers.yaml`."""
    entry = marketplace_entry(server_name)
    if entry is None:
        names = ", ".join(server.name for server in marketplace_entries())
        raise click.ClickException(f"Unknown MCP server {server_name!r}. Available: {names}")

    catalog_path = _catalog_path()
    previous = {server.name: server for server in load_catalog_entries(catalog_path)}
    _, created = upsert_catalog_entry(catalog_path, entry)
    previous_entry = previous.get(entry.name)

    if created:
        console.print(f"[green]Installed[/green] MCP server [bold]{entry.name}[/bold] into [dim]{catalog_path}[/dim]")
    elif previous_entry == entry:
        console.print(f"[cyan]Already installed[/cyan] MCP server [bold]{entry.name}[/bold]")
    else:
        console.print(f"[yellow]Updated[/yellow] MCP server [bold]{entry.name}[/bold] in [dim]{catalog_path}[/dim]")


@mcp_server.command("test")
@click.argument("server_name")
@click.option(
    "--json-output",
    is_flag=True,
    default=False,
    help="Emit the protocol report as JSON instead of Rich text.",
)
def test_server(server_name: str, json_output: bool) -> None:
    """Validate an installed MCP server with a bounded protocol smoke suite."""
    from bernstein.core.mcp_protocol_test import resolve_catalog_server, run_protocol_test

    catalog_path = _catalog_path()
    entry = resolve_catalog_server(server_name, catalog_path)
    if entry is None:
        installed_names = ", ".join(server.name for server in load_catalog_entries(catalog_path))
        if installed_names:
            raise click.ClickException(f"Unknown MCP server {server_name!r}. Installed: {installed_names}")
        raise click.ClickException(
            "No MCP catalog entries found. Install one first with `bernstein mcp install <server-name>`."
        )

    report = asyncio.run(run_protocol_test(entry, cwd=Path.cwd()))
    if report.tool_reports:
        write_tool_inventory_snapshot(
            Path.cwd() / ".sdd",
            entry.name,
            [
                {
                    "name": tool_report.name,
                    "required_arguments": list(tool_report.required_arguments),
                    "input_schema_valid": tool_report.input_schema_valid,
                    "output_schema_valid": tool_report.output_schema_valid,
                }
                for tool_report in report.tool_reports
            ],
        )

    if json_output:
        console.print_json(json.dumps(report.to_dict()))
    else:
        _print_protocol_report(report)

    if not report.passed:
        raise click.ClickException(
            f"MCP protocol validation failed for {entry.name} ({len(report.failures)} issue(s))."
        )


@mcp_server.command("usage")
@click.option(
    "--json-output",
    is_flag=True,
    default=False,
    help="Emit the historical usage report as JSON instead of Rich tables.",
)
def usage_report(json_output: bool) -> None:
    """Analyze historical MCP usage and print optimization recommendations."""
    report = analyze_mcp_usage(Path.cwd() / ".sdd", catalog_path=_catalog_path())
    if json_output:
        console.print_json(json.dumps(report.to_dict()))
        return
    _print_usage_report(report)


def _print_protocol_report(report: Any) -> None:
    """Render a human-readable MCP protocol validation report."""
    from rich.table import Table

    console.print(
        f"[bold cyan]MCP Protocol Test[/bold cyan] [bold]{report.server_name}[/bold] ([dim]{report.transport}[/dim])"
    )
    console.print(
        f"Tools: [bold]{report.tool_count}[/bold]  "
        f"Unknown tool rejection: [bold]{_render_status(report.unknown_tool_rejected)}[/bold]  "
        f"Invalid arguments: [bold]{_render_optional_status(report.invalid_arguments_rejected)}[/bold]  "
        f"Empty args edge case: [bold]{_render_optional_status(report.empty_arguments_supported)}[/bold]"
    )
    console.print(f"Duration: [dim]{report.duration_seconds:.2f}s[/dim]")

    table = Table(title="Validated Tools", show_header=True, header_style="bold cyan")
    table.add_column("Tool")
    table.add_column("Required args")
    table.add_column("Input schema")
    table.add_column("Output schema")

    for tool_report in report.tool_reports:
        table.add_row(
            tool_report.name,
            ", ".join(tool_report.required_arguments) or "--",
            _render_status(tool_report.input_schema_valid),
            _render_status(tool_report.output_schema_valid),
        )

    console.print(table)

    if report.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for warning in report.warnings:
            console.print(f"  - {warning}")

    if report.failures:
        console.print("[red]Failures:[/red]")
        for failure in report.failures:
            console.print(f"  - {failure}")
    else:
        console.print("[green]Protocol validation passed.[/green]")


def _render_status(value: bool) -> str:
    """Render a pass/fail state for terminal output."""
    return "[green]pass[/green]" if value else "[red]fail[/red]"


def _render_optional_status(value: bool | None) -> str:
    """Render a pass/fail/skip state for terminal output."""
    if value is None:
        return "[yellow]skip[/yellow]"
    return _render_status(value)


def _print_usage_report(report: MCPUsageAnalyticsReport) -> None:
    """Render a human-readable historical MCP usage report."""
    from rich.table import Table

    console.print("[bold cyan]MCP Usage Analytics[/bold cyan]")
    console.print(
        f"Recorded tool calls: [bold]{report.total_calls}[/bold]  "
        f"Installed servers: [bold]{len(report.installed_servers)}[/bold]"
    )

    servers_table = Table(title="Server Summary", show_header=True, header_style="bold cyan")
    servers_table.add_column("Server")
    servers_table.add_column("Installed")
    servers_table.add_column("Calls")
    servers_table.add_column("Used tools")
    servers_table.add_column("Unused tools")
    servers_table.add_column("Error rate")
    servers_table.add_column("Avg latency (ms)")

    for server in report.servers:
        servers_table.add_row(
            server.server_name,
            "yes" if server.installed else "no",
            str(server.total_calls),
            str(server.distinct_tools_used),
            str(len(server.unused_tools)),
            f"{server.error_rate:.0%}",
            f"{server.avg_latency_ms:.2f}",
        )

    console.print(servers_table)

    top_tools_table = Table(title="Top Tools", show_header=True, header_style="bold cyan")
    top_tools_table.add_column("Server")
    top_tools_table.add_column("Tool")
    top_tools_table.add_column("Calls")
    top_tools_table.add_column("Error rate")
    top_tools_table.add_column("Avg latency (ms)")

    for tool in report.top_tools[:10]:
        top_tools_table.add_row(
            tool.server_name,
            tool.tool_name,
            str(tool.total_calls),
            f"{tool.error_rate:.0%}",
            f"{tool.avg_latency_ms:.2f}",
        )

    console.print(top_tools_table)

    if report.recommendations:
        console.print("[yellow]Recommendations:[/yellow]")
        for recommendation in report.recommendations:
            console.print(f"  - {recommendation.message}")
    else:
        console.print("[green]No optimization recommendations right now.[/green]")
