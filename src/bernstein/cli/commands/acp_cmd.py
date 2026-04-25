"""CLI surface for the Agent Client Protocol (ACP) bridge.

Exposes ``bernstein acp serve --stdio`` (default; for IDE embedding) and
``bernstein acp serve --http :PORT`` (for remote IDEs and debugging).

Stdio is the canonical IDE transport: the editor spawns ``bernstein acp
serve --stdio`` as a subprocess and communicates via line-delimited
JSON-RPC.  HTTP is provided for remote and CI usage; it rides the
existing tunnel wrapper for non-loopback access.
"""

from __future__ import annotations

import asyncio
import logging

import click

from bernstein.cli.helpers import SERVER_URL, console
from bernstein.core.protocols.acp.server import (
    ACPServer,
    build_default_server,
)

logger = logging.getLogger(__name__)


@click.group("acp")
def acp_group() -> None:
    """Agent Client Protocol (ACP) bridge — IDE integration surface."""


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
    default=SERVER_URL,
    show_default=True,
    help="URL of the running Bernstein task server.",
)
def serve(use_stdio: bool, http_addr: str | None, server_url: str) -> None:
    """Run the ACP server using the requested transport.

    The ``--stdio`` flag is the default for IDE embedding; supply
    ``--http :PORT`` to switch to HTTP/SSE.  ``--http`` overrides
    ``--stdio`` when both are supplied.
    """
    server = build_default_server(server_url=server_url)
    if http_addr:
        host, port = _parse_addr(http_addr)
        console.print(f"[cyan]ACP[/cyan] HTTP transport on [bold]{host}:{port}[/bold] (backend: {server_url})")
        asyncio.run(_run_http(server, host, port))
        return
    if not use_stdio:
        raise click.UsageError("either --stdio or --http :PORT must be enabled")
    asyncio.run(server.run_stdio())


def _parse_addr(addr: str) -> tuple[str, int]:
    """Parse ``[host]:port`` into ``(host, port)``.

    Args:
        addr: The address string.

    Returns:
        ``(host, port)`` — host defaults to ``127.0.0.1`` when omitted.

    Raises:
        click.UsageError: If the address is malformed.
    """
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
        raise click.UsageError(f"port {port} out of range")
    return host, port


async def _run_http(server: ACPServer, host: str, port: int) -> None:
    """Start a tiny HTTP/1.1 server that delegates to :class:`HttpAcpTransport`.

    Uses :mod:`asyncio` directly to avoid pulling FastAPI / Starlette into
    the import path of every CLI invocation.  The server understands a
    single route (``POST /acp``) and exits on Ctrl-C.
    """
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
    try:
        async with httpd:
            await httpd.serve_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


async def _read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    """Read one HTTP/1.1 request from *reader*.

    Returns ``None`` on EOF.
    """
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
    merged = {"content-length": str(len(body)), "connection": "close", **headers}
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
    chunks: object,
) -> None:
    """Write an HTTP/1.1 chunked-transfer streaming response.

    *chunks* is an async iterator yielding bytes; each chunk becomes one
    HTTP chunk.
    """
    header_lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}"]
    merged = {
        "transfer-encoding": "chunked",
        "connection": "close",
        **headers,
    }
    for key, value in merged.items():
        header_lines.append(f"{key}: {value}")
    writer.write(("\r\n".join(header_lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()
    async for chunk in chunks:  # type: ignore[union-attr]
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


__all__ = ["acp_group", "serve"]
