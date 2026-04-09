"""CLI group for Bernstein MCP server and marketplace management."""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import SERVER_URL, console
from bernstein.core.mcp_marketplace import marketplace_entries, marketplace_entry
from bernstein.core.mcp_registry import load_catalog_entries, upsert_catalog_entry


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
