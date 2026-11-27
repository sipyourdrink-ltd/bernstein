"""CLI surface for the Agent Client Protocol (ACP) bridge.

Exposes ``bernstein acp serve --stdio`` (default; embedded workspace runner),
``bernstein acp serve --stdio --setup`` (terminal-auth adapter check), and
``bernstein acp serve --http :PORT`` (HTTP/SSE transport).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import console
from bernstein.core.protocols.acp.runner import (
    adapter_binary_name,
    find_installed_cli_adapters,
    is_adapter_usable,
)
from bernstein.core.protocols.acp.server import (
    ACPServer,
    build_default_server,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

logger = logging.getLogger(__name__)


def _acp_cli_choice() -> click.Choice[str]:
    """Return ``click.Choice`` of selectable adapters plus ``auto`` and ``mock``."""
    from bernstein.adapters.registry import selectable_adapter_names

    return click.Choice(sorted({*selectable_adapter_names(), "auto", "mock"}), case_sensitive=False)


def run_terminal_setup(
    *,
    adapter_name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Verify local adapter availability for ACP ``terminal`` authentication.

    Returns ``0`` when the requested CLI adapter (or at least one selectable CLI
    adapter when unpinned) is available; returns ``1`` with guidance otherwise.
    """
    eff_env = env if env is not None else os.environ
    pinned = (adapter_name or eff_env.get("BERNSTEIN_DEFAULT_CLI") or "").strip().lower()
    if pinned == "mock":
        click.echo("Bernstein ACP setup: mock adapter active (ready).", err=True)
        return 0

    if pinned and pinned != "auto":
        if is_adapter_usable(pinned, env=eff_env):
            click.echo(f"Bernstein ACP setup: adapter '{pinned}' found in PATH (ready).", err=True)
            return 0
        binary = adapter_binary_name(pinned)
        click.echo(
            f"Bernstein ACP setup: adapter '{pinned}' (binary '{binary}') not found in PATH. "
            "Install the adapter CLI or run 'bernstein connect' to configure credentials.",
            err=True,
        )
        return 1

    installed = find_installed_cli_adapters(eff_env)
    if installed:
        click.echo(
            f"Bernstein ACP setup: found usable adapter(s): {', '.join(installed)}.",
            err=True,
        )
        return 0

    click.echo(
        "Bernstein ACP setup: no supported CLI adapter found in PATH. "
        "Install claude/codex/gemini or run 'bernstein connect' to configure a provider.",
        err=True,
    )
    return 1


@click.group("acp", invoke_without_command=True)
@click.option(
    "--setup",
    "run_setup",
    is_flag=True,
    default=False,
    help="Verify local CLI adapters and credentials for ACP terminal authentication.",
)
@click.option(
    "--cli",
    "adapter_name",
    type=_acp_cli_choice(),
    default=None,
    help="Adapter CLI to validate or run (defaults to auto-detect).",
)
@click.pass_context
def acp_group(ctx: click.Context, run_setup: bool, adapter_name: str | None) -> None:
    """Agent Client Protocol (ACP) bridge: IDE integration surface."""
    if run_setup and ctx.invoked_subcommand is None:
        ctx.exit(run_terminal_setup(adapter_name=adapter_name))
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@acp_group.command("serve")
@click.option(
    "--stdio/--no-stdio",
    "use_stdio",
    default=True,
    show_default=True,
    help="Serve over POSIX stdio (line-delimited JSON-RPC).",
)
@click.option(
    "--http",
    "http_addr",
    default=None,
    metavar="HOST:PORT",
    help="Serve over HTTP/SSE on HOST:PORT (e.g. ':8062' or '127.0.0.1:8062').",
)
@click.option(
    "--server-url",
    default=lambda: os.environ.get("BERNSTEIN_SERVER_URL") or None,
    show_default=False,
    help="Forward tasks to an external Bernstein task server URL instead of the embedded runner.",
)
@click.option(
    "--cli",
    "adapter_name",
    type=_acp_cli_choice(),
    default=None,
    help="Adapter CLI for embedded workspace execution.",
)
@click.option(
    "--setup",
    "run_setup",
    is_flag=True,
    default=False,
    help="Verify local CLI adapters and credentials for ACP terminal authentication and exit.",
)
@click.pass_context
def serve(
    ctx: click.Context,
    use_stdio: bool,
    http_addr: str | None,
    server_url: str | None,
    adapter_name: str | None,
    run_setup: bool,
) -> None:
    """Run the ACP server using the requested transport."""
    if run_setup:
        ctx.exit(run_terminal_setup(adapter_name=adapter_name))

    server = build_default_server(server_url=server_url, adapter_name=adapter_name)
    if http_addr:
        host, port = _parse_addr(http_addr)
        backend_label = server_url or "embedded"
        console.print(f"[cyan]ACP[/cyan] HTTP transport on [bold]{host}:{port}[/bold] (backend: {backend_label})")
        asyncio.run(_run_http(server, host, port))
        return
    if not use_stdio:
        raise click.UsageError("Enable --stdio or provide --http HOST:PORT.")
    asyncio.run(server.run_stdio())


def _parse_addr(addr: str) -> tuple[str, int]:
    """Parse ``[host]:port`` into ``(host, port)``."""
    raw = addr.strip()
    if not raw:
        raise click.UsageError("--http requires a HOST:PORT or :PORT argument")
    if raw.startswith(":"):
        host = "127.0.0.1"
        port_str = raw[1:]
    elif ":" in raw:
        host, _, port_str = raw.rpartition(":")
        host = host or "127.0.0.1"
    else:
        host = "127.0.0.1"
        port_str = raw
    try:
        port = int(port_str)
    except ValueError as exc:
        raise click.UsageError(f"invalid port {port_str!r}") from exc
    if not (0 < port < 65536):
        raise click.UsageError(f"Port {port} is out of range.")
    return host, port


async def _run_http(server: ACPServer, host: str, port: int) -> None:
    """Start a tiny HTTP/1.1 server that delegates to :class:`HttpAcpTransport`."""
    transport = server.http_transport()

    async def _handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await _read_http_request(reader)
        except Exception as exc:
            logger.debug("acp.http malformed request: %s", exc)
            await _write_status(writer, 400, b"bad request")
            writer.close()
            return

        peer_ip = writer.get_extra_info("peername", ("?", 0))[0]
        if request is None:
            writer.close()
            return

        method, path, headers, body = request
        if method != "POST" or not path.startswith("/acp"):
            await _write_status(writer, 404, b"not found")
            writer.close()
            return

        accept = headers.get("accept", "")
        status, response_headers, body_or_iter = await transport.handle_request(body, accept, peer=f"http://{peer_ip}")
        if isinstance(body_or_iter, (bytes, bytearray)):
            await _write_response(writer, status, response_headers, bytes(body_or_iter))
        else:
            await _write_streaming_response(writer, status, response_headers, body_or_iter)

        try:
            await writer.drain()
        finally:
            writer.close()

    httpd = await asyncio.start_server(_handle_client, host, port)
    with suppress(KeyboardInterrupt, asyncio.CancelledError):
        async with httpd:
            await httpd.serve_forever()


async def _read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    """Read one HTTP/1.1 request from *reader*."""
    request_line = await reader.readline()
    if not request_line:
        return None
    parts = request_line.decode("ascii", errors="replace").strip().split()
    if len(parts) < 2:
        return None
    method, path = parts[0].upper(), parts[1]

    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        decoded = line.decode("latin-1").rstrip("\r\n")
        if ":" in decoded:
            key, _, value = decoded.partition(":")
            headers[key.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0") or 0)
    body = await reader.readexactly(length) if length > 0 else b""
    return method, path, headers, body


async def _write_status(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
    """Write a minimal HTTP/1.1 response with no body negotiation."""
    response = (
        f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\n"
        f"content-length: {len(body)}\r\n"
        f"content-type: text/plain\r\n"
        f"connection: close\r\n"
        f"\r\n"
    ).encode("latin-1") + body
    writer.write(response)
    await writer.drain()


async def _write_response(
    writer: asyncio.StreamWriter,
    status: int,
    headers: dict[str, str],
    body: bytes,
) -> None:
    """Write a non-streaming HTTP response."""
    header_lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}"]
    merged = {
        "content-length": str(len(body)),
        "connection": "close",
    } | headers
    for key, value in merged.items():
        header_lines.append(f"{key}: {value}")
    writer.write(("\r\n".join(header_lines) + "\r\n\r\n").encode("latin-1"))
    if body:
        writer.write(body)
    await writer.drain()


async def _write_streaming_response(
    writer: asyncio.StreamWriter,
    status: int,
    headers: dict[str, str],
    chunks: AsyncIterator[bytes],
) -> None:
    """Write an HTTP/1.1 chunked-transfer streaming response."""
    header_lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}"]
    merged = {
        "transfer-encoding": "chunked",
        "connection": "close",
    } | headers
    for key, value in merged.items():
        header_lines.append(f"{key}: {value}")
    writer.write(("\r\n".join(header_lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()
    async for chunk in chunks:
        writer.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
        await writer.drain()
    writer.write(b"0\r\n\r\n")
    await writer.drain()


_REASONS: dict[int, str] = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    404: "Not Found",
}


__all__ = ["acp_group", "run_terminal_setup", "serve"]
