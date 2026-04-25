"""Composition root for the ACP bridge.

Wires the schema layer, handler registry, session store, transport, and
the existing Bernstein primitives (task store, drain pipeline, HMAC
audit chain, janitor approval gate) together.

The high-level call graph::

    bernstein acp serve --stdio
        -> build_default_server()          # composes everything
        -> ACPServer.run_stdio()           # wires sys.stdin/stdout
            -> StdioAcpTransport.serve_forever()
                -> registry.dispatch()
                    -> task_creator()      # POST /tasks on the task server
                    -> audit_emitter()     # AuditLog.log()
                    -> permission_asker()  # janitor gate

In tests we instantiate :class:`ACPServer` directly with stub callables
so the full request flow is exercised without a real task server.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Any

from bernstein.core.protocols.acp.handlers import (
    ACPHandlerRegistry,
    AuditEmitter,
    PromptResult,
    StreamPublisher,
    TaskCanceller,
    TaskCreator,
)
from bernstein.core.protocols.acp.metrics import set_active_sessions
from bernstein.core.protocols.acp.session import ACPSessionStore
from bernstein.core.protocols.acp.transport import (
    HttpAcpTransport,
    StdioAcpTransport,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdapterDescriptor:
    """Lightweight descriptor for an adapter surfaced via ``initialize``.

    Attributes:
        name: Adapter id (e.g. ``"claude"``).
        display_name: Optional user-visible name.
    """

    name: str
    display_name: str = ""


@dataclass(frozen=True)
class SandboxBackendDescriptor:
    """Lightweight descriptor for a configured sandbox backend.

    Attributes:
        name: Backend id (e.g. ``"docker"``, ``"firejail"``, ``"none"``).
        available: Whether the backend is usable on the current host.
    """

    name: str
    available: bool = True


@dataclass(frozen=True)
class ServerCapabilities:
    """Snapshot of the capabilities the server reports during ``initialize``.

    Attributes:
        adapters: Adapter descriptors.
        sandbox_backends: Sandbox backend descriptors.
    """

    adapters: tuple[AdapterDescriptor, ...] = ()
    sandbox_backends: tuple[SandboxBackendDescriptor, ...] = ()


@dataclass
class ACPServer:
    """High-level entry point for running the ACP bridge.

    Attributes:
        registry: The bound handler registry.  Constructed by
            :func:`build_default_server` from injected callables.
    """

    registry: ACPHandlerRegistry

    async def run_stdio(
        self,
        reader: asyncio.StreamReader | None = None,
        writer: asyncio.StreamWriter | None = None,
    ) -> None:
        """Serve ACP over stdio until the input stream closes.

        Args:
            reader: Optional pre-built reader.  When ``None`` the method
                attaches to ``sys.stdin``.
            writer: Optional pre-built writer.  When ``None`` the method
                attaches to ``sys.stdout``.
        """
        if reader is None or writer is None:
            reader, writer = await _stdio_streams()

        transport = StdioAcpTransport(
            registry=self.registry,
            reader=reader,
            writer=writer,
        )
        await transport.serve_forever()

    def http_transport(self) -> HttpAcpTransport:
        """Return an :class:`HttpAcpTransport` bound to this server."""
        return HttpAcpTransport(registry=self.registry)


# ---------------------------------------------------------------------------
# Stdio stream attachment (POSIX-only; ticket non-goals exclude Windows
# named pipes for v1.9).
# ---------------------------------------------------------------------------


async def _stdio_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Attach asyncio stream wrappers to ``sys.stdin`` and ``sys.stdout``.

    When the shell redirects stdin from a regular file (the common
    debugging case ``bernstein acp serve --stdio < fixture.jsonl``),
    :func:`asyncio.AbstractEventLoop.connect_read_pipe` rejects the
    file because it is not a pipe/socket.  We fall back to a background
    thread that reads the file and pumps bytes into an in-memory
    :class:`asyncio.StreamReader`.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(loop=loop)

    try:
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except (ValueError, OSError):
        # Regular-file fallback: spawn a daemon thread to drain stdin.
        import threading

        thread = threading.Thread(
            target=_drain_into_reader,
            args=(sys.stdin, reader, loop),
            daemon=True,
        )
        thread.start()

    try:
        transport, protocol_w = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
        writer = asyncio.StreamWriter(transport, protocol_w, reader, loop)
    except (ValueError, OSError):
        writer = _SyncStdoutWriter()  # type: ignore[assignment]
    return reader, writer


def _drain_into_reader(
    source: Any,
    reader: asyncio.StreamReader,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Drain *source* (a regular file-like object) into *reader*.

    Schedules ``feed_data`` on *loop* via :meth:`call_soon_threadsafe`
    so the asyncio side observes incoming bytes.  Closes the reader on
    EOF.
    """
    try:
        while True:
            buffer = getattr(source, "buffer", source)
            chunk = buffer.read(8192)
            if isinstance(chunk, str):
                chunk = chunk.encode()
            if not chunk:
                break
            loop.call_soon_threadsafe(reader.feed_data, chunk)
    except Exception:
        pass
    finally:
        loop.call_soon_threadsafe(reader.feed_eof)


class _SyncStdoutWriter:
    """Minimal asyncio-StreamWriter-like adapter for a regular stdout file.

    Used only when the shell redirects ``stdout`` to a non-pipe; the
    common IDE-embedding path takes the real pipe transport.
    """

    def write(self, data: bytes) -> None:
        """Write *data* to the underlying ``sys.stdout`` buffer."""
        sys.stdout.buffer.write(data)

    async def drain(self) -> None:
        """Flush ``sys.stdout`` synchronously."""
        sys.stdout.flush()

    def close(self) -> None:
        """Flush and ignore further writes."""
        import contextlib

        with contextlib.suppress(Exception):
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# Default wiring helpers
# ---------------------------------------------------------------------------


def build_default_server(
    *,
    server_url: str = "http://127.0.0.1:8052",
    adapters: tuple[AdapterDescriptor, ...] | None = None,
    sandbox_backends: tuple[SandboxBackendDescriptor, ...] | None = None,
    audit_emitter: AuditEmitter | None = None,
    task_creator: TaskCreator | None = None,
    task_canceller: TaskCanceller | None = None,
    stream_publisher: StreamPublisher | None = None,
) -> ACPServer:
    """Build an :class:`ACPServer` with sensible defaults.

    The defaults reach the running Bernstein task server over HTTP.  Any
    callable can be overridden — tests inject in-memory stubs; the
    production CLI command threads through the real audit log.

    Args:
        server_url: Base URL of the running Bernstein task server.
            Defaults match :data:`bernstein.cli.helpers.SERVER_URL`.
        adapters: Adapter descriptors to surface during ``initialize``.
            When ``None``, queries the adapter registry.
        sandbox_backends: Sandbox backends to surface during
            ``initialize``.  When ``None``, ships an empty list.
        audit_emitter: Override for the HMAC audit emitter.  When
            ``None``, audit events go to the standard logger only —
            production CLI overrides with a real :class:`AuditLog`.
        task_creator: Override for task creation.
        task_canceller: Override for task cancellation.
        stream_publisher: Override for stream publication.

    Returns:
        A fully composed :class:`ACPServer`.
    """
    if adapters is None:
        adapters = _discover_adapters()
    if sandbox_backends is None:
        sandbox_backends = _discover_sandbox_backends()

    if task_creator is None:
        task_creator = _http_task_creator(server_url)
    if task_canceller is None:
        task_canceller = _http_task_canceller(server_url)

    sessions = ACPSessionStore()
    set_active_sessions(0)

    registry = ACPHandlerRegistry(
        sessions=sessions,
        adapters=tuple(d.name for d in adapters),
        sandbox_backends=tuple(d.name for d in sandbox_backends if d.available),
        task_creator=task_creator,
        task_canceller=task_canceller,
    )
    if audit_emitter is not None:
        registry.audit_emitter = audit_emitter
    if stream_publisher is not None:
        registry.stream_publisher = stream_publisher

    return ACPServer(registry=registry)


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _discover_adapters() -> tuple[AdapterDescriptor, ...]:
    """Return adapters registered with the runtime registry, sorted by name."""
    try:
        from bernstein.adapters import registry as adapter_registry

        names = sorted(getattr(adapter_registry, "_ADAPTERS", {}))
        return tuple(AdapterDescriptor(name=name) for name in names)
    except Exception:
        logger.debug("acp.discover_adapters failed; returning empty list", exc_info=True)
        return ()


def _discover_sandbox_backends() -> tuple[SandboxBackendDescriptor, ...]:
    """Return the configured sandbox backends in priority order."""
    # The sandbox subsystem is feature-flagged and discovered at runtime.
    # We return a conservative default list so the IDE knows what is
    # negotiable.  Production deployments can override via
    # build_default_server(sandbox_backends=...).
    return (
        SandboxBackendDescriptor(name="none", available=True),
        SandboxBackendDescriptor(name="firejail", available=False),
        SandboxBackendDescriptor(name="docker", available=False),
    )


# ---------------------------------------------------------------------------
# HTTP-driven default task creator + canceller
# ---------------------------------------------------------------------------


def _http_task_creator(server_url: str) -> TaskCreator:
    """Return a :data:`TaskCreator` that POSTs to the task server."""

    async def _create(prompt: str, cwd: str, role: str) -> PromptResult:
        try:
            import httpx
        except ImportError:
            return PromptResult(session_id="", accepted=False, message="httpx not available")

        payload: dict[str, Any] = {
            "title": prompt[:120],
            "description": prompt,
            "role": role,
            "scope": "small",
            "complexity": "medium",
            "priority": 3,
            "metadata": {"source": "acp", "cwd": cwd},
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(f"{server_url}/tasks", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("acp.create_task failed: %s", exc)
            return PromptResult(session_id="", accepted=False, message=str(exc))

        sid = data.get("id") or data.get("task_id") or data.get("session_id") or ""
        if not sid:
            return PromptResult(session_id="", accepted=False, message="missing session id in response")
        return PromptResult(session_id=str(sid), accepted=True)

    return _create


def _http_task_canceller(server_url: str) -> TaskCanceller:
    """Return a :data:`TaskCanceller` that POSTs to ``/tasks/{id}/cancel``."""

    async def _cancel(session_id: str, reason: str) -> bool:
        try:
            import httpx
        except ImportError:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{server_url}/tasks/{session_id}/cancel",
                    json={"reason": reason},
                )
                return 200 <= resp.status_code < 300
        except Exception as exc:
            logger.warning("acp.cancel_task failed: %s", exc)
            return False

    return _cancel
