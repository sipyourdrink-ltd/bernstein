"""Composition root for the ACP bridge.

Wires the schema layer, handler registry, session store, transport, and
the existing Bernstein primitives together:

* **Standalone mode (default when ``server_url`` is ``None``)**: runs
  :class:`~bernstein.core.protocols.acp.runner.EmbeddedOrchestratorRunner`
  in-process against ``<cwd>/.sdd`` with live :class:`EventJournal` ->
  ``session/update`` streaming and :class:`ApprovalQueue` ->
  ``session/request_permission`` bridging.
* **Remote server mode (``--server-url``)**: forwards task creation and
  cancellation over HTTP with :func:`~bernstein.core.run_auth_token.auth_headers`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from bernstein.core.protocols.acp.handlers import (
    ACPHandlerRegistry,
    PromptResult,
    PromptRunOutcome,
)
from bernstein.core.protocols.acp.metrics import set_active_sessions
from bernstein.core.protocols.acp.runner import EmbeddedOrchestratorRunner
from bernstein.core.protocols.acp.session import ACPSession, ACPSessionStore
from bernstein.core.protocols.acp.transport import (
    MAX_FRAME_BYTES,
    HttpAcpTransport,
    StdioAcpTransport,
)
from bernstein.core.run_auth_token import auth_headers

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

DEFAULT_TERMINAL_AUTH_METHOD: Final[dict[str, Any]] = {
    "id": "bernstein-setup",
    "name": "Configure Bernstein adapters",
    "description": "Verify local CLI adapters and credentials via bernstein setup",
    "type": "terminal",
    "args": ["--setup"],
}

_UNSET_AUTH: Final[object] = object()


@dataclass(frozen=True)
class AdapterDescriptor:
    """Lightweight descriptor for an adapter surfaced via ``initialize``."""

    name: str
    display_name: str = ""


@dataclass(frozen=True)
class SandboxBackendDescriptor:
    """Lightweight descriptor for a configured sandbox backend."""

    name: str
    available: bool = True


@dataclass(frozen=True)
class ServerCapabilities:
    """Snapshot of the capabilities the server reports during ``initialize``."""

    adapters: tuple[AdapterDescriptor, ...] = ()
    sandbox_backends: tuple[SandboxBackendDescriptor, ...] = ()


@dataclass
class ACPServer:
    """High-level entry point for running the ACP bridge.

    Attributes:
        registry: The bound handler registry.  Constructed by
            :func:`build_default_server` from injected callables.
        eof_drain_grace_s: Seconds to wait on stdin half-close (EOF) for
            in-flight ``session/prompt`` turns to finish and flush their
            responses before signalling ``transport_eof`` cancellation.
        eof_cancel_timeout_s: Additional seconds to wait for cancelled turns
            to resolve cleanly before hard-cancelling them on shutdown.
    """

    registry: ACPHandlerRegistry
    eof_drain_grace_s: float = 0.5
    eof_cancel_timeout_s: float = 0.5

    async def run_stdio(
        self,
        reader: asyncio.StreamReader | None = None,
        writer: asyncio.StreamWriter | None = None,
    ) -> None:
        """Serve ACP over stdio until the input stream closes."""
        if reader is None or writer is None:
            reader, writer = await _stdio_streams()

        transport = StdioAcpTransport(
            registry=self.registry,
            reader=reader,
            writer=writer,
            eof_drain_grace_s=self.eof_drain_grace_s,
            eof_cancel_timeout_s=self.eof_cancel_timeout_s,
        )
        await transport.serve_forever()

    def http_transport(self) -> HttpAcpTransport:
        """Return an :class:`HttpAcpTransport` bound to this server.

        Raises:
            ValueError: If ``self.registry.require_auth`` is ``True`` (HTTP
                requires token-bound authentication before enforcing auth).
        """
        return HttpAcpTransport(registry=self.registry)


async def _stdio_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Attach asyncio stream wrappers to ``sys.stdin`` and ``sys.stdout``."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES + 2, loop=loop)

    try:
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except (ValueError, OSError):
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
        writer = cast("asyncio.StreamWriter", _SyncStdoutWriter())
    return reader, writer


def _drain_into_reader(
    source: Any,
    reader: asyncio.StreamReader,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Drain *source* (a regular file-like object) into *reader*."""
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
    """Minimal asyncio-StreamWriter-like adapter for a regular stdout file."""

    def write(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)

    async def drain(self) -> None:
        sys.stdout.flush()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            sys.stdout.flush()


def build_default_server(
    *,
    server_url: str | None = None,
    adapter_name: str | None = None,
    adapters: tuple[AdapterDescriptor, ...] | None = None,
    sandbox_backends: tuple[SandboxBackendDescriptor, ...] | None = None,
    audit_emitter: Callable[[str, str, dict[str, Any]], None] | None = None,
    task_creator: Callable[[str, str, str], Awaitable[PromptResult]] | None = None,
    task_canceller: Callable[[str, str], Awaitable[bool]] | None = None,
    stream_publisher: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    prompt_runner: (
        Callable[[ACPSession, list[dict[str, Any]], asyncio.Event], Awaitable[str | PromptRunOutcome]] | None
    ) = None,
    auth_handler: Callable[[str], Awaitable[bool]] | None = None,
    terminal_auth_method: dict[str, Any] | object | None = _UNSET_AUTH,
    require_auth: bool = False,
    eof_drain_grace_s: float = 0.5,
    eof_cancel_timeout_s: float = 0.5,
) -> ACPServer:
    """Build an :class:`ACPServer` in standalone embedded mode or HTTP proxy mode."""
    if adapters is None:
        adapters = _discover_adapters()
    if sandbox_backends is None:
        sandbox_backends = _discover_sandbox_backends()

    resolved_terminal_auth: dict[str, Any] | None
    if terminal_auth_method is _UNSET_AUTH:
        resolved_terminal_auth = dict(DEFAULT_TERMINAL_AUTH_METHOD)
    elif isinstance(terminal_auth_method, dict):
        resolved_terminal_auth = dict(cast("dict[str, Any]", terminal_auth_method))
    else:
        resolved_terminal_auth = None

    sessions = ACPSessionStore()
    set_active_sessions(0)

    use_remote = bool(server_url and server_url.strip())
    effective_creator = task_creator
    effective_canceller = task_canceller
    remote_task_cwds: dict[str, str] = {}

    if use_remote:
        assert server_url is not None
        if effective_creator is None:
            effective_creator = _http_task_creator(server_url, task_cwds=remote_task_cwds)
        if effective_canceller is None:
            effective_canceller = _http_task_canceller(server_url, sessions=sessions, task_cwds=remote_task_cwds)

    registry = ACPHandlerRegistry(
        sessions=sessions,
        adapters=tuple(d.name for d in adapters),
        sandbox_backends=tuple(d.name for d in sandbox_backends if d.available),
        prompt_runner=prompt_runner,
        auth_handler=auth_handler,
        terminal_auth_method=resolved_terminal_auth,
        require_auth=require_auth,
    )
    if effective_creator is not None:
        registry.task_creator = effective_creator
    if effective_canceller is not None:
        registry.task_canceller = effective_canceller

    if not use_remote and prompt_runner is None and task_creator is None:
        embedded = EmbeddedOrchestratorRunner(registry=registry, adapter_name=adapter_name)
        registry.prompt_runner = embedded.run_prompt
        registry.task_creator = embedded.create_legacy_task
        if effective_canceller is None:
            registry.task_canceller = embedded.cancel_session

    if audit_emitter is not None:
        registry.audit_emitter = audit_emitter
    if stream_publisher is not None:
        registry.stream_publisher = stream_publisher

    return ACPServer(
        registry=registry,
        eof_drain_grace_s=eof_drain_grace_s,
        eof_cancel_timeout_s=eof_cancel_timeout_s,
    )


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
    return (
        SandboxBackendDescriptor(name="none", available=True),
        SandboxBackendDescriptor(name="firejail", available=False),
        SandboxBackendDescriptor(name="docker", available=False),
    )


def _resolve_request_headers(cwd: str | None) -> dict[str, str]:
    """Resolve ``auth_headers`` for *cwd*, falling back to process-level headers."""
    if cwd:
        ws_headers = auth_headers(Path(cwd))
        if ws_headers:
            return ws_headers
    return auth_headers()


def _http_task_creator(
    server_url: str,
    *,
    task_cwds: dict[str, str] | None = None,
) -> Callable[[str, str, str], Awaitable[PromptResult]]:
    """Return a ``TaskCreator`` that POSTs to *server_url* with ``auth_headers()``."""

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
        headers = _resolve_request_headers(cwd)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(f"{server_url.rstrip('/')}/tasks", json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("acp.create_task failed: %s", exc)
            return PromptResult(session_id="", accepted=False, message=str(exc))

        sid = data.get("id") or data.get("task_id") or data.get("session_id") or ""
        if not sid:
            return PromptResult(session_id="", accepted=False, message="missing session id in response")
        sid_str = str(sid)
        if task_cwds is not None and cwd:
            task_cwds[sid_str] = cwd
        return PromptResult(session_id=sid_str, accepted=True)

    return _create


def _http_task_canceller(
    server_url: str,
    *,
    sessions: ACPSessionStore | None = None,
    task_cwds: dict[str, str] | None = None,
) -> Callable[[str, str], Awaitable[bool]]:
    """Return a ``TaskCanceller`` that POSTs to ``/tasks/{id}/cancel`` with ``auth_headers()``."""

    async def _cancel(task_or_session_id: str, reason: str) -> bool:
        try:
            import httpx
        except ImportError:
            return False

        cwd: str | None = task_cwds.pop(task_or_session_id, None) if task_cwds is not None else None
        if cwd is None and sessions is not None:
            sess = await sessions.get(task_or_session_id)
            if sess is not None:
                cwd = sess.cwd

        headers = _resolve_request_headers(cwd)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{server_url.rstrip('/')}/tasks/{task_or_session_id}/cancel",
                    json={"reason": reason},
                    headers=headers,
                )
                return 200 <= resp.status_code < 300
        except Exception as exc:
            logger.warning("acp.cancel_task failed: %s", exc)
            return False

    return _cancel
