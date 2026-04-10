"""Gateway CLI commands — start and replay MCP proxy sessions.

Commands:
    bernstein gateway start --upstream <cmd>   Transparent MCP proxy (stdio or SSE).
    bernstein gateway replay <run-id>          Replay recorded tool calls from WAL.
"""

from __future__ import annotations

import asyncio
import shlex
import uuid
from pathlib import Path

import click

from bernstein.cli.helpers import console

_DEFAULT_PORT = 8054
_SDD_DIR = Path(".sdd")


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group("gateway")
def gateway_group() -> None:
    """MCP gateway proxy — transparent recording and replay."""


# ---------------------------------------------------------------------------
# gateway start
# ---------------------------------------------------------------------------


@gateway_group.command("start")
@click.option(
    "--upstream",
    required=True,
    metavar="CMD",
    help="Shell command to start the upstream MCP server (e.g. 'uvx mcp-server-git').",
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    show_default=True,
    help="Transport mode: stdio (default) or sse (HTTP).",
)
@click.option(
    "--port",
    default=_DEFAULT_PORT,
    show_default=True,
    help="Port for SSE transport (ignored in stdio mode).",
)
@click.option(
    "--run-id",
    "run_id",
    default=None,
    metavar="ID",
    help="WAL run ID (auto-generated if not provided).",
)
@click.option(
    "--server-name",
    default="unknown",
    show_default=True,
    help="Logical MCP server name recorded into the gateway WAL for historical analytics.",
)
def start_cmd(upstream: str, transport: str, port: int, run_id: str | None, server_name: str) -> None:
    """Start the MCP gateway proxy.

    In stdio mode the gateway acts as an MCP stdio server — point your MCP
    client at ``bernstein gateway start --upstream <cmd>`` instead of the
    real server command.

    In SSE mode the gateway listens on ``--port`` and proxies to the upstream
    via its stdio transport.

    \b
    Examples:
        bernstein gateway start --upstream "uvx mcp-server-git"
        bernstein gateway start --upstream "npx @modelcontextprotocol/server-filesystem ." \\
            --transport sse --port 8054
    """
    from bernstein.core.mcp_gateway import MCPGateway
    from bernstein.core.wal import WALWriter

    effective_run_id = run_id or f"gw-{uuid.uuid4().hex[:8]}"
    sdd_dir = _SDD_DIR
    sdd_dir.mkdir(parents=True, exist_ok=True)

    wal_writer = WALWriter(run_id=effective_run_id, sdd_dir=sdd_dir)
    upstream_cmd = shlex.split(upstream)
    gateway = MCPGateway(upstream_cmd=upstream_cmd, wal_writer=wal_writer, server_name=server_name)

    if transport == "sse":
        console.print(
            f"[bold green]MCP Gateway[/bold green] starting"
            f" — SSE on [cyan]http://127.0.0.1:{port}[/cyan]"
            f"  (run-id: [dim]{effective_run_id}[/dim])"
        )
        console.print(f"  Upstream: [cyan]{upstream}[/cyan]")
        console.print(f"  Server: [cyan]{server_name}[/cyan]")
    else:
        # stdio: suppress console output — stdout is the MCP transport
        pass

    asyncio.run(_run_gateway(gateway, transport=transport, port=port))


# ---------------------------------------------------------------------------
# gateway replay
# ---------------------------------------------------------------------------


@gateway_group.command("replay")
@click.argument("run_id")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    show_default=True,
    help="Transport mode.",
)
@click.option(
    "--port",
    default=_DEFAULT_PORT,
    show_default=True,
    help="Port for SSE transport.",
)
def replay_cmd(run_id: str, transport: str, port: int) -> None:
    """Replay recorded MCP tool calls from a previous gateway run.

    Serves responses from the WAL without connecting to an upstream server —
    useful for offline debugging and development.

    \b
    Example:
        bernstein gateway replay gw-abc12345
        bernstein audit show   # shows recorded run IDs
    """
    from bernstein.core.mcp_gateway import GatewayReplay, MCPGateway
    from bernstein.core.wal import WALWriter

    sdd_dir = _SDD_DIR
    wal_path = sdd_dir / "runtime" / "wal" / f"{run_id}.wal.jsonl"
    if not wal_path.exists():
        console.print(f"[red]WAL not found:[/red] {wal_path}")
        console.print("[dim]Use 'bernstein audit show' to list recorded runs.[/dim]")
        raise SystemExit(1)

    replay = GatewayReplay(run_id=run_id, sdd_dir=sdd_dir)
    replay_run_id = f"replay-{run_id}"
    wal_writer = WALWriter(run_id=replay_run_id, sdd_dir=sdd_dir)
    gateway = MCPGateway(upstream_cmd=[], wal_writer=wal_writer, replay=replay)

    if transport == "sse":
        console.print(
            f"[bold blue]MCP Replay[/bold blue] — SSE on [cyan]http://127.0.0.1:{port}[/cyan]"
            f"  (source: [dim]{run_id}[/dim], {replay.indexed_count} patterns indexed)"
        )

    asyncio.run(_run_gateway(gateway, transport=transport, port=port))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _run_gateway(gateway: MCPGateway, *, transport: str, port: int) -> None:  # noqa: F821
    """Start the gateway and block until done."""
    from bernstein.core.mcp_gateway import MCPGateway, create_gateway_sse_app

    assert isinstance(gateway, MCPGateway)
    await gateway.start()
    run_id = getattr(gateway._wal_writer, "_path", "unknown")

    try:
        if transport == "stdio":
            await gateway.run_stdio()
        else:
            import uvicorn

            run_id_str = str(run_id).rsplit("/", 1)[-1].replace(".wal.jsonl", "")
            app = create_gateway_sse_app(gateway, run_id=run_id_str)
            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
            server = uvicorn.Server(config)
            await server.serve()
    finally:
        await gateway.stop()
