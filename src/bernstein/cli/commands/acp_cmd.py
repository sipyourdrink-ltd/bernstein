"""CLI surface for the Agent Client Protocol (ACP) bridge.

Exposes ``bernstein acp serve --stdio`` (default; embedded workspace runner),
``bernstein acp serve --stdio --setup`` (terminal-auth adapter check), and
``bernstein acp serve --http :PORT`` (HTTP/SSE transport).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import click

from bernstein.cli.helpers import SERVER_URL, console
from bernstein.core.protocols.acp.runner import (
    adapter_binary_name,
    find_installed_cli_adapters,
    is_adapter_usable,
)
from bernstein.core.protocols.acp.server import (
    ACPServer,
    build_default_server,
)
from bernstein.core.protocols.acp.transport import MAX_FRAME_BYTES
from bernstein.core.server.dashboard_tokens import is_loopback_host

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
@click.option(
    "--allow-embedded-http",
    "allow_embedded_http",
    is_flag=True,
    default=False,
    help="Allow the embedded workspace runner on --http (loopback binds only).",
)
@click.pass_context
def serve(
    ctx: click.Context,
    use_stdio: bool,
    http_addr: str | None,
    server_url: str | None,
    adapter_name: str | None,
    run_setup: bool,
    allow_embedded_http: bool = False,
) -> None:
    """Run the ACP server using the requested transport."""
    if run_setup:
        ctx.exit(run_terminal_setup(adapter_name=adapter_name))

    if http_addr:
        host, port = _parse_addr(http_addr)
        if not is_loopback_host(host):
            raise click.UsageError(
                f"Refusing to bind unauthenticated ACP HTTP transport to non-loopback host {host!r}; "
                "bind to 127.0.0.1, ::1, or localhost (and use a tunnel wrapper for remote access)."
            )
        if allow_embedded_http:
            if ctx.get_parameter_source("server_url") == click.core.ParameterSource.COMMANDLINE and server_url:
                raise click.UsageError("Cannot combine --allow-embedded-http with --server-url.")
            effective_server_url: str | None = None
        else:
            effective_server_url = server_url or SERVER_URL
        server = build_default_server(
            server_url=effective_server_url,
            adapter_name=adapter_name,
            forward_auth_headers=False,
        )
        backend_label = effective_server_url or "embedded (loopback)"
        console.print(f"[cyan]ACP[/cyan] HTTP transport on [bold]{host}:{port}[/bold] (backend: {backend_label})")
        asyncio.run(_run_http(server, host, port))
        return

    if allow_embedded_http:
        raise click.UsageError("--allow-embedded-http requires --http HOST:PORT.")
    if not use_stdio:
        raise click.UsageError("Enable --stdio or provide --http HOST:PORT.")
    server = build_default_server(server_url=server_url, adapter_name=adapter_name)
    asyncio.run(server.run_stdio())


_BRACKETED_HOST_RE = re.compile(r"\A\[([^\]]+)\](?::(\d+))?\Z")


def _parse_addr(addr: str) -> tuple[str, int]:
    """Parse ``[host]:port`` into ``(host, port)``."""
    raw = addr.strip()
    if not raw:
        raise click.UsageError("--http requires a HOST:PORT or :PORT argument")
    if raw.startswith("["):
        m = _BRACKETED_HOST_RE.match(raw)
        if m is None or not m.group(2):
            raise click.UsageError(f"invalid bracketed address {raw!r}; expected [HOST]:PORT")
        host = m.group(1)
        port_str = m.group(2)
    elif raw.startswith(":"):
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


def _is_allowed_http_host_header(headers: dict[str, str]) -> bool:
    """Return ``True`` only when ``Host`` is present once and names a loopback host."""
    if "_duplicate_host" in headers:
        return False
    raw = headers.get("host", "").strip()
    if not raw or "," in raw or any(ch.isspace() for ch in raw):
        return False
    if raw.startswith("["):
        m = _BRACKETED_HOST_RE.match(raw)
        if m is None:
            return False
        host_part, port_part = m.group(1), m.group(2)
        if port_part is not None:
            port_num = int(port_part)
            if not (0 < port_num < 65536):
                return False
        return is_loopback_host(host_part)
    if raw.count(":") > 1:
        return False
    if ":" in raw:
        host_part, _, port_part = raw.partition(":")
        if not port_part.isdigit():
            return False
        port_num = int(port_part)
        if not (0 < port_num < 65536):
            return False
    else:
        host_part = raw
    return is_loopback_host(host_part)


def _is_allowed_http_origin_header(headers: dict[str, str]) -> bool:
    """Return ``True`` when ``Origin`` is absent, or present once with a loopback origin."""
    if "_duplicate_origin" in headers:
        return False
    if "origin" not in headers:
        return True
    raw = headers["origin"].strip()
    if not raw or raw.lower() == "null" or "," in raw or any(ch.isspace() for ch in raw):
        return False
    try:
        parsed = urlsplit(raw)
        port_val = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return False
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return False
    if port_val is not None and not (0 < port_val < 65536):
        return False
    return is_loopback_host(parsed.hostname)


async def _run_http(server: ACPServer, host: str, port: int) -> None:
    """Start a loopback-only HTTP/1.1 server that delegates to :class:`HttpAcpTransport`."""
    if not is_loopback_host(host):
        raise ValueError(
            f"Refusing to bind unauthenticated ACP HTTP transport to non-loopback host {host!r}; "
            "bind to 127.0.0.1, ::1, or localhost."
        )
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
        if not _is_allowed_http_host_header(headers):
            await _write_status(writer, 403, b"forbidden host")
            writer.close()
            return
        if not _is_allowed_http_origin_header(headers):
            await _write_status(writer, 403, b"forbidden origin")
            writer.close()
            return
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
            norm_key = key.strip().lower()
            if norm_key in {"host", "origin"} and norm_key in headers:
                headers[f"_duplicate_{norm_key}"] = "1"
            headers[norm_key] = value.strip()

    length = int(headers.get("content-length", "0") or 0)
    if length < 0 or length > MAX_FRAME_BYTES * 2:
        raise ValueError(f"invalid content-length {length}")
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
    403: "Forbidden",
    404: "Not Found",
}


__all__ = ["acp_group", "run_terminal_setup", "serve"]
