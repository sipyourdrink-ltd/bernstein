"""MCP client for consuming remote MCP tools.

Connects to remote MCP servers via streamable HTTP transport,
discovers available tools, and calls them on behalf of Bernstein agents.

The client is stateless on the wire (issue #2506): it never mints or
round-trips a protocol session id. Every request carries a per-request
``_meta`` built by :func:`bernstein.core.protocols.mcp.stateless_core.
build_request_meta` - client capabilities plus W3C Trace Context whose ids
are derived from content hashes - so two replays of the same run emit
byte-identical ``_meta`` and any server instance can serve any request.
An ``input_required`` tool result is retried by echoing the server's
``requestState`` back on the follow-up call, so the retry can land on a
different instance with no shared memory.

Upstream MCP servers are treated as untrusted, brittle, and rate-limited
(issue #1673). The client hardens every tool call against the failure modes
real servers exhibit:

* **Capability-card validation** -- before issuing a tool call the client
  verifies the tool is declared in the server's manifest (or runtime
  capability card). A mismatch raises :class:`MCPCapabilityMissing`, logged
  with the manifest digest.
* **Retry-with-continuation** -- if a streamed tool call drops mid-stream the
  client resumes from the server's last-checkpoint token. Servers that do not
  advertise resumption fall back to a full retry carrying an idempotency key.
* **Streamed-output cancellation** -- an in-flight call can be cancelled
  without leaking the underlying request; partial output is preserved.
* **Per-server cost-meter** -- metered calls accumulate into the shared
  :mod:`bernstein.core.cost` subsystem, attributed per server per task.
* **Schema-violation containment** -- malformed responses are caught, logged,
  surfaced through the metrics tracker, and the server is marked degraded for
  the remainder of the task.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx

from bernstein.core.protocols.mcp.stateless_core import build_request_meta, decode_request_state

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from bernstein.core.cost.mcp_server_cost import MCPServerCostMeter
    from bernstein.core.protocols.mcp.mcp_metrics import MCPMetricsCollector

logger = logging.getLogger(__name__)

_CONTENT_TYPE_JSON = "application/json"
_CONTENT_TYPE_SSE = "text/event-stream"


@dataclass(frozen=True)
class RemoteServerConfig:
    """Configuration for a remote MCP server connection.

    Attributes:
        name: Human-readable identifier for the server.
        url: Base URL of the remote MCP server.
        transport: Transport type - ``"streamable-http"`` or ``"sse"``.
        auth_type: Authentication method - ``"none"``, ``"bearer"``, or ``"oauth"``.
        auth_token: Bearer token when auth_type is ``"bearer"``.
        oauth_client_id: OAuth client ID when auth_type is ``"oauth"``.
        oauth_client_secret: OAuth client secret when auth_type is ``"oauth"``.
        timeout_seconds: Request timeout in seconds.
        retry_limit: Maximum number of retries for failed requests.
        max_continuation_retries: Maximum retry-with-continuation attempts
            for a streamed tool call that drops mid-stream (issue #1673).
        validate_capabilities: When True, every tool call is checked against
            the discovered manifest before dispatch; a mismatch raises
            :class:`MCPCapabilityMissing`.
    """

    name: str
    url: str
    transport: str = "streamable-http"
    auth_type: str = "none"
    auth_token: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    timeout_seconds: int = 30
    retry_limit: int = 3
    max_continuation_retries: int = 3
    validate_capabilities: bool = True


@dataclass(frozen=True)
class RemoteTool:
    """A tool discovered from a remote MCP server.

    Attributes:
        name: Tool name as reported by the server.
        description: Human-readable tool description.
        server_name: Name of the server that hosts this tool.
        input_schema: JSON Schema describing the tool's input parameters.
    """

    name: str
    description: str
    server_name: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallResult:
    """Result from calling a remote MCP tool.

    Attributes:
        content: Text content returned by the tool.
        is_error: Whether the tool call resulted in an error.
        metadata: Additional metadata from the response.
    """

    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class MCPClientError(Exception):
    """Base error for MCP client operations."""


class MCPConnectionError(MCPClientError):
    """Raised when connection to remote MCP server fails."""


class MCPAuthError(MCPClientError):
    """Raised when authentication with remote MCP server fails."""


class MCPToolNotFoundError(MCPClientError):
    """Raised when a requested tool is not found on the server."""


class MCPCapabilityMissing(MCPToolNotFoundError):
    """Raised when a tool is not declared in the server's capability card.

    Capability-card validation (issue #1673, AC1) runs before every tool
    call. When the requested tool is absent from the discovered manifest the
    client refuses to dispatch and raises this error rather than trusting an
    undeclared capability. The manifest digest is logged so the mismatch can
    be correlated against the manifest the client validated against.

    Subclasses :class:`MCPToolNotFoundError` because an undeclared capability
    is, semantically, a tool that is not available; callers catching the
    broader error keep working while callers that want the manifest-digest
    context catch this subclass.
    """


class MCPSchemaViolation(MCPClientError):
    """Raised when a server response is structurally malformed.

    Covers invalid JSON, a missing JSON-RPC envelope, and tool results that
    omit required fields. The client contains the violation, marks the server
    degraded for the rest of the task (issue #1673, AC5), and surfaces it via
    the metrics tracker.
    """


class MCPStreamDropped(MCPClientError):
    """Raised internally when a streamed tool call drops mid-stream.

    Drives retry-with-continuation (issue #1673, AC2). Carries the last
    checkpoint token the server emitted, if any, so the client can request a
    resume rather than replaying the whole call.

    Attributes:
        resumption_token: Last-checkpoint token from the server, or ``None``
            when the server does not support resumption.
        partial_content: Text accumulated before the drop.
    """

    def __init__(
        self,
        message: str,
        *,
        resumption_token: str | None = None,
        partial_content: str = "",
    ) -> None:
        super().__init__(message)
        self.resumption_token = resumption_token
        self.partial_content = partial_content


@dataclass(frozen=True)
class StreamChunk:
    """One chunk of a streamed tool-call response (issue #1673).

    Attributes:
        text: Text payload of the chunk (appended to partial output).
        checkpoint_token: Server-issued resumption token, if this chunk
            advances the last-checkpoint. ``None`` leaves the token unchanged.
        final: Whether this chunk completes the stream.
        dropped: Whether the transport signalled a mid-stream drop. Drives
            retry-with-continuation.
    """

    text: str = ""
    checkpoint_token: str | None = None
    final: bool = False
    dropped: bool = False


@dataclass
class StreamedToolCall:
    """Handle on an in-flight streamed tool call (issue #1673, AC3).

    Accumulates streamed chunks and exposes a cooperative ``cancel`` that
    stops consuming the stream without leaking the underlying request. The
    text seen before cancellation is preserved on :attr:`partial_content`.

    Attributes:
        server_name: Server the call is running against.
        tool_name: Tool being invoked.
        partial_content: Text accumulated so far (preserved on cancel).
        cancelled: Whether the call has been cancelled.
        resumption_token: Last checkpoint token observed, if any.
    """

    server_name: str
    tool_name: str
    partial_content: str = ""
    cancelled: bool = False
    resumption_token: str | None = None
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def cancel(self) -> None:
        """Request cancellation of the in-flight call.

        Idempotent and safe to call from another task. The consuming loop
        observes the flag at the next chunk boundary, stops, and preserves
        :attr:`partial_content`.
        """
        self.cancelled = True
        self._cancel_event.set()

    @property
    def is_cancel_requested(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancel_event.is_set()


#: Methods a stateless server anchors into the audit chain. The client
#: advances its anchor-specific call index only for these so the baggage
#: ``mcp.call_index`` claim matches the server's journal allocation: a
#: non-anchored request (``initialize``, ``tools/list``, notifications) must
#: not consume an audit ordinal, or the chained call_index sequence would gap.
_ANCHOR_ELIGIBLE_METHODS = frozenset({"tools/call"})


class MCPClientSession:
    """Active client connection to a remote MCP server.

    Handles the JSON-RPC 2.0 protocol over HTTP, including tool discovery
    and tool invocation. No protocol session id is minted or round-tripped
    (issue #2506): every request carries a content-derived ``_meta`` and the
    server is expected to serve it from the body alone.

    Args:
        config: Configuration for the remote server.
        cost_meter: Optional per-server cost meter (issue #1673).
        metrics: Optional metrics collector for latency / error events.
        task_id: Task identifier stamped onto cost-meter entries.
        run_root_hash: Root hash seeding the run-scoped W3C trace id in
            every request ``_meta``. Pass the run journal's genesis hash to
            connect wire traces to the run; when omitted, a deterministic
            projection of the server identity is used so replays still emit
            byte-identical ``_meta``.
        client_capabilities: Capability map advertised per request in
            ``_meta`` (the stateless replacement for the ``initialize``
            handshake advertisement).
    """

    def __init__(
        self,
        config: RemoteServerConfig,
        *,
        cost_meter: MCPServerCostMeter | None = None,
        metrics: MCPMetricsCollector | None = None,
        task_id: str = "",
        run_root_hash: str = "",
        client_capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        self._config = config
        self._tools: list[RemoteTool] = []
        self._initialized: bool = False
        self._request_id: int = 0
        # Stateless call identity (issue #2506): the trace id is run-scoped
        # and the span id is derived per call from content hash + ordered
        # index, so no wire value depends on process-local randomness.
        self._run_root_hash = run_root_hash or self._derive_default_run_root(config)
        self._client_capabilities: dict[str, Any] = dict(client_capabilities or {})
        # Per-message counter seeding the span id (every message gets a
        # distinct span). Separate from the anchor-specific index below.
        self._call_index: int = 0
        # Ordered position among audit-anchored calls, carried in baggage as
        # ``mcp.call_index``. Advances only for anchor-eligible methods so the
        # claim stays contiguous with the server's journal allocation (AC5).
        self._anchor_call_index: int = 0
        # Hardening state (issue #1673).
        self._manifest_digest: str = ""
        self._degraded: bool = False
        self._degraded_reason: str = ""
        self._cost_meter = cost_meter
        self._metrics = metrics
        self._task_id = task_id

    @staticmethod
    def _derive_default_run_root(config: RemoteServerConfig) -> str:
        """Return a deterministic run root derived from the server identity."""
        canonical = json.dumps(
            {"server": config.name, "url": config.url},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def server_name(self) -> str:
        """Name of the connected server."""
        return self._config.name

    @property
    def run_root_hash(self) -> str:
        """Root hash seeding the run-scoped trace id in every ``_meta``."""
        return self._run_root_hash

    @property
    def tools(self) -> list[RemoteTool]:
        """List of discovered tools (copy)."""
        return self._tools.copy()

    @property
    def is_connected(self) -> bool:
        """Whether the session has been initialized."""
        return self._initialized

    @property
    def manifest_digest(self) -> str:
        """SHA-256 digest of the last discovered tool manifest."""
        return self._manifest_digest

    @property
    def is_degraded(self) -> bool:
        """Whether the server has been marked degraded for this task."""
        return self._degraded

    @property
    def degraded_reason(self) -> str:
        """Human-readable reason the server was marked degraded."""
        return self._degraded_reason

    def mark_degraded(self, reason: str) -> None:
        """Mark the server degraded for the rest of the task (AC5).

        Idempotent: the first reason is retained. Surfaced through the
        metrics tracker when one is wired in.
        """
        if not self._degraded:
            self._degraded = True
            self._degraded_reason = reason
            logger.warning("MCP server '%s' marked degraded: %s", self._config.name, reason)
            if self._metrics is not None:
                self._metrics.record_availability(self._config.name, alive=False)

    async def connect(self) -> None:
        """Connect to the remote server and discover its tools.

        Sends the legacy ``initialize`` request followed by an
        ``initialized`` notification for servers that still expect the
        handshake (the stateless spec revision removes it), then discovers
        available tools. No session id is captured from the response: the
        client's capabilities already ride in every request ``_meta``, so
        connecting is a discovery convenience, not a protocol state change.

        Raises:
            MCPConnectionError: If the server cannot be reached.
            MCPAuthError: If authentication fails.
        """
        init_result = await self._send_jsonrpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": self._client_capabilities.copy(),
                "clientInfo": {
                    "name": "bernstein",
                    "version": "1.0.0",
                },
            },
        )

        logger.info(
            "MCP client connected to server '%s': %s",
            self._config.name,
            init_result.get("serverInfo", {}),
        )

        # Send initialized notification (no response expected)
        await self._send_notification("notifications/initialized")

        self._initialized = True

        # Discover tools
        await self.list_tools()

    async def list_tools(self) -> list[RemoteTool]:
        """Discover available tools from remote server.

        Sends ``tools/list`` and caches the result.

        Returns:
            List of discovered remote tools.

        Raises:
            MCPClientError: If the request fails.
        """
        result = await self._send_jsonrpc("tools/list")
        raw_tools = result.get("tools", [])
        if not isinstance(raw_tools, list):
            self.mark_degraded("tools/list returned a non-list 'tools' field")
            raise MCPSchemaViolation(f"Server '{self._config.name}' returned a malformed tools/list payload")
        tools_list = cast("list[Any]", raw_tools)

        self._tools = []
        for entry in tools_list:
            if not isinstance(entry, dict) or not entry.get("name"):
                self.mark_degraded("tools/list entry missing required 'name' field")
                raise MCPSchemaViolation(f"Server '{self._config.name}' returned a tool entry without a name")
            tool_data = cast("dict[str, Any]", entry)
            input_schema = tool_data.get("inputSchema", {})
            tool = RemoteTool(
                name=str(tool_data.get("name", "")),
                description=str(tool_data.get("description", "")),
                server_name=self._config.name,
                input_schema=cast("dict[str, Any]", input_schema) if isinstance(input_schema, dict) else {},
            )
            self._tools.append(tool)

        self._manifest_digest = self._compute_manifest_digest(tools_list)
        logger.info(
            "Discovered %d tools from server '%s' (manifest digest %s)",
            len(self._tools),
            self._config.name,
            self._manifest_digest[:12],
        )
        return self._tools.copy()

    @staticmethod
    def _compute_manifest_digest(raw_tools: list[Any]) -> str:
        """Return a stable SHA-256 digest of the tool manifest.

        Used to correlate capability mismatches (AC1) with the exact
        manifest the client validated against.
        """
        canonical = json.dumps(raw_tools, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _validate_capability(self, tool_name: str) -> None:
        """Verify ``tool_name`` is declared in the server's capability card.

        Raises:
            MCPCapabilityMissing: When the tool is absent from the discovered
                manifest and capability validation is enabled.
        """
        if not self._config.validate_capabilities:
            return
        known_names = {t.name for t in self._tools}
        if known_names and tool_name not in known_names:
            logger.warning(
                "Capability mismatch on server '%s': tool '%s' not in manifest (digest %s); available=%s",
                self._config.name,
                tool_name,
                self._manifest_digest[:12] or "<none>",
                sorted(known_names),
            )
            raise MCPCapabilityMissing(
                f"Tool '{tool_name}' not found in the capability card of "
                f"server '{self._config.name}' (manifest digest "
                f"{self._manifest_digest[:12] or '<none>'})"
            )

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        cost_usd: float = 0.0,
        request_state: str | None = None,
    ) -> ToolCallResult:
        """Call a tool on the remote server with full hardening (issue #1673).

        Runs capability-card validation before dispatch, contains
        schema-violating responses (marking the server degraded), records a
        per-server cost-meter entry, and surfaces latency / error to the
        metrics tracker.

        An ``input_required`` result surfaces the server's ``requestState``
        echo on ``metadata['request_state']``. Retry by calling again with
        ``request_state=<that echo>`` and the supplied input as
        ``arguments``: the echoed state carries everything needed to resume,
        so the retry may be served by a different server instance with no
        shared memory (issue #2506).

        Args:
            tool_name: Name of the tool to call.
            arguments: Arguments to pass to the tool.
            cost_usd: Metered cost to attribute to this call (default 0).
            request_state: Opaque ``requestState`` echoed by a prior
                ``input_required`` result, when resuming that call.

        Returns:
            Result of the tool call.

        Raises:
            MCPCapabilityMissing: If the tool is not in the capability card.
            MCPToolNotFoundError: If the tool is not found on this server.
            MCPSchemaViolation: If the response is structurally malformed.
            MCPClientError: If the call fails.
            ValueError: If ``request_state`` is not a valid echo.
        """
        self._validate_capability(tool_name)

        # ``known_names`` is empty before discovery; once tools are known a
        # missing entry that passed capability validation (validation off)
        # is still a hard not-found error.
        known_names = {t.name for t in self._tools}
        if known_names and tool_name not in known_names:
            raise MCPToolNotFoundError(
                f"Tool '{tool_name}' not found on server '{self._config.name}'. Available: {sorted(known_names)}"
            )

        params: dict[str, Any] = {"name": tool_name, "arguments": arguments}
        if request_state is not None:
            # Fail fast on a corrupted echo before it reaches the wire.
            decode_request_state(request_state)
            params["requestState"] = request_state

        started = time.monotonic()
        errored = False
        try:
            result = await self._send_jsonrpc("tools/call", params)
            tool_result = self._parse_tool_result(result, tool_name)
            errored = tool_result.is_error
            return tool_result
        except MCPSchemaViolation:
            errored = True
            raise
        except MCPClientError:
            errored = True
            raise
        finally:
            self._record_call_telemetry(
                tool_name=tool_name,
                latency_ms=(time.monotonic() - started) * 1000.0,
                errored=errored,
                cost_usd=cost_usd,
            )

    def _parse_tool_result(self, result: dict[str, Any], tool_name: str) -> ToolCallResult:
        """Parse a ``tools/call`` result, containing schema violations (AC5).

        ``result`` is already a JSON object (``_send_jsonrpc`` rejects a
        non-object ``result`` field). This method additionally guards the
        ``content`` block shape.

        An ``input_required`` result is not an error: the server's
        ``requestState`` echo and prompt surface on the metadata so the
        caller can resume the call (issue #2506). The echo must itself be a
        present, decodable ``requestState`` -- a resume built on a missing or
        corrupt state can never complete, so it is treated as a schema
        violation rather than surfaced as a resumable prompt.

        Raises:
            MCPSchemaViolation: When the ``content`` block is structurally
                malformed, or an ``input_required`` result omits a decodable
                ``requestState``. The server is marked degraded before the
                error propagates.
        """
        if result.get("type") == "input_required":
            raw_state = result.get("requestState")
            if not isinstance(raw_state, str) or not raw_state:
                self.mark_degraded(f"tools/call for '{tool_name}' returned input_required without a requestState")
                raise MCPSchemaViolation(
                    f"Server '{self._config.name}' returned an input_required result without a requestState "
                    f"for tool '{tool_name}'"
                )
            try:
                decode_request_state(raw_state)
            except ValueError as exc:
                self.mark_degraded(f"tools/call for '{tool_name}' returned an undecodable requestState")
                raise MCPSchemaViolation(
                    f"Server '{self._config.name}' returned an undecodable requestState for tool '{tool_name}'"
                ) from exc
            prompt = str(result.get("prompt", ""))
            return ToolCallResult(
                content=prompt,
                is_error=False,
                metadata={
                    "server": self._config.name,
                    "tool": tool_name,
                    "input_required": True,
                    "prompt": prompt,
                    "request_state": raw_state,
                },
            )
        content_parts = result.get("content", [])
        if not isinstance(content_parts, list):
            self.mark_degraded(f"tools/call for '{tool_name}' returned non-list content")
            raise MCPSchemaViolation(f"Server '{self._config.name}' returned malformed content for tool '{tool_name}'")
        parts = cast("list[Any]", content_parts)
        text_parts: list[str] = [
            str(cast("dict[str, Any]", part).get("text", ""))
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return ToolCallResult(
            content="\n".join(text_parts) if text_parts else json.dumps(result),
            is_error=bool(result.get("isError", False)),
            metadata={"server": self._config.name, "tool": tool_name},
        )

    def _record_call_telemetry(
        self,
        *,
        tool_name: str,
        latency_ms: float,
        errored: bool,
        cost_usd: float,
    ) -> None:
        """Feed latency / error / cost into the metrics + cost subsystems."""
        if self._metrics is not None:
            self._metrics.record_call(
                self._config.name,
                tool_name,
                latency_ms,
                error=errored,
            )
        if self._cost_meter is not None and cost_usd > 0.0:
            self._cost_meter.record(
                task_id=self._task_id,
                server_name=self._config.name,
                tool_name=tool_name,
                cost_usd=cost_usd,
            )

    # ------------------------------------------------------------------
    # Streamed tool calls with cancellation + retry-with-continuation
    # (issue #1673, AC2 + AC3)
    # ------------------------------------------------------------------

    async def call_tool_streaming(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        stream_factory: Callable[[str | None, str], AsyncIterator[StreamChunk]],
        handle: StreamedToolCall | None = None,
        cost_usd: float = 0.0,
    ) -> ToolCallResult:
        """Call a tool over a stream, surviving mid-stream drops (AC2 + AC3).

        Chunks are produced by ``stream_factory``, which is invoked with
        ``(resumption_token, idempotency_key)``. On the first attempt the
        resumption token is ``None``; if the stream drops and the server
        emitted a checkpoint token the client resumes from it, otherwise it
        replays the whole call carrying the same idempotency key so the
        server can dedupe. Retries are bounded by
        ``config.max_continuation_retries``.

        Cancellation: pass a :class:`StreamedToolCall` ``handle`` and call
        ``handle.cancel()`` from another task. The consuming loop stops at the
        next chunk boundary; partial output is preserved on the handle and
        returned.

        Args:
            tool_name: Name of the tool to call.
            arguments: Arguments to pass to the tool.
            stream_factory: Factory yielding :class:`StreamChunk` objects.
            handle: Optional caller-owned handle for cancellation / inspection.
            cost_usd: Metered cost to attribute to this call.

        Returns:
            The accumulated tool result. ``metadata['cancelled']`` is ``True``
            when the call was cancelled mid-stream.

        Raises:
            MCPCapabilityMissing: If the tool is not in the capability card.
            MCPStreamDropped: If retries are exhausted before completion.
        """
        _ = arguments
        self._validate_capability(tool_name)
        call = handle if handle is not None else StreamedToolCall(self._config.name, tool_name)
        idempotency_key = str(uuid.uuid4())

        started = time.monotonic()
        errored = False
        try:
            attempts = self._config.max_continuation_retries + 1
            for attempt in range(attempts):
                resume_from = call.resumption_token
                try:
                    completed = await self._consume_stream(
                        call=call,
                        stream=stream_factory(resume_from, idempotency_key),
                    )
                except MCPStreamDropped as drop:
                    call.resumption_token = drop.resumption_token or call.resumption_token
                    if attempt >= attempts - 1:
                        errored = True
                        self.mark_degraded(f"streamed tool '{tool_name}' dropped and retries exhausted")
                        raise
                    logger.warning(
                        "Streamed tool '%s' on '%s' dropped (attempt %d/%d); resuming %s",
                        tool_name,
                        self._config.name,
                        attempt + 1,
                        attempts,
                        "from checkpoint" if call.resumption_token else "with full retry",
                    )
                    continue

                if call.is_cancel_requested:
                    return ToolCallResult(
                        content=call.partial_content,
                        is_error=False,
                        metadata={"server": self._config.name, "tool": tool_name, "cancelled": True},
                    )
                if completed:
                    return ToolCallResult(
                        content=call.partial_content,
                        is_error=False,
                        metadata={"server": self._config.name, "tool": tool_name, "cancelled": False},
                    )
            # Loop exhausted without completion or drop signal.
            errored = True
            raise MCPStreamDropped(
                f"Streamed tool '{tool_name}' on '{self._config.name}' did not complete",
                resumption_token=call.resumption_token,
                partial_content=call.partial_content,
            )
        finally:
            self._record_call_telemetry(
                tool_name=tool_name,
                latency_ms=(time.monotonic() - started) * 1000.0,
                errored=errored,
                cost_usd=cost_usd,
            )

    async def _consume_stream(
        self,
        *,
        call: StreamedToolCall,
        stream: AsyncIterator[StreamChunk],
    ) -> bool:
        """Consume a chunk stream into ``call``; return True when it completed.

        Honours cooperative cancellation, tracks the latest checkpoint token,
        and raises :class:`MCPStreamDropped` if the stream signals a drop.
        """
        async for chunk in stream:
            if call.is_cancel_requested:
                logger.info(
                    "Streamed tool '%s' on '%s' cancelled; %d chars preserved",
                    call.tool_name,
                    self._config.name,
                    len(call.partial_content),
                )
                return False
            if chunk.checkpoint_token is not None:
                call.resumption_token = chunk.checkpoint_token
            if chunk.dropped:
                raise MCPStreamDropped(
                    f"Stream for tool '{call.tool_name}' dropped mid-stream",
                    resumption_token=call.resumption_token,
                    partial_content=call.partial_content,
                )
            if chunk.text:
                call.partial_content += chunk.text
            if chunk.final:
                return True
        # Stream ended without a final marker -> treat as a drop so the
        # caller can retry-with-continuation rather than silently truncating.
        raise MCPStreamDropped(
            f"Stream for tool '{call.tool_name}' ended without a final chunk",
            resumption_token=call.resumption_token,
            partial_content=call.partial_content,
        )

    async def close(self) -> None:
        """Close the MCP session."""
        self._initialized = False
        self._tools = []
        logger.info("Closed MCP session with server '%s'", self._config.name)

    def _next_request_meta(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """Build the per-request ``_meta`` and advance the ordered indices.

        The span id is derived from the call's content hash and its per-message
        index, the trace id from the run root, so two replays of the same call
        sequence emit byte-identical ``_meta`` (issue #2506). The baggage
        ``mcp.call_index`` carries a separate anchor-specific index that
        advances only for anchor-eligible methods, so the claim it presents to
        a server matches that server's journal allocation and the anchored
        call_index sequence stays contiguous (AC5).
        """
        canonical = json.dumps(
            {"method": method, "params": params or {}},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        params_content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        meta = build_request_meta(
            method=method,
            params_content_hash=params_content_hash,
            run_root_hash=self._run_root_hash,
            call_index=self._call_index,
            client_capabilities=self._client_capabilities,
            anchor_index=self._anchor_call_index,
        )
        self._call_index += 1
        if method in _ANCHOR_ELIGIBLE_METHODS:
            self._anchor_call_index += 1
        return meta

    async def _send_jsonrpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send JSON-RPC request to remote server.

        Every request carries a content-derived ``_meta``; no session header
        is attached or captured (issue #2506).

        Args:
            method: JSON-RPC method name.
            params: Optional parameters for the method.

        Returns:
            The ``result`` field from the JSON-RPC response.

        Raises:
            MCPConnectionError: If the server cannot be reached.
            MCPAuthError: If authentication fails (401/403).
            MCPClientError: If the server returns a JSON-RPC error.
        """
        self._request_id += 1
        params_with_meta: dict[str, Any] = dict(params) if params is not None else {}
        params_with_meta["_meta"] = self._next_request_meta(method, params)
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params_with_meta,
        }

        headers = {
            "Content-Type": _CONTENT_TYPE_JSON,
            "Accept": _CONTENT_TYPE_JSON,
        } | self._build_auth_headers()

        last_error: Exception | None = None
        for attempt in range(self._config.retry_limit):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(self._config.timeout_seconds)) as client:
                    response = await client.post(
                        self._config.url,
                        json=payload,
                        headers=headers,
                    )

                if response.status_code in (401, 403):
                    raise MCPAuthError(
                        f"Authentication failed for server '{self._config.name}': HTTP {response.status_code}"
                    )

                response.raise_for_status()

                # Any legacy session header a server still sends is ignored:
                # continuity is chain-anchored, never session-anchored.

                # Schema-violation containment (AC5): invalid JSON or a
                # missing JSON-RPC envelope is a malformed response. Mark the
                # server degraded and surface a typed error rather than
                # leaking a raw decode exception.
                try:
                    data: Any = response.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    self.mark_degraded(f"{method} returned invalid JSON")
                    raise MCPSchemaViolation(
                        f"Server '{self._config.name}' returned invalid JSON for '{method}': {exc}"
                    ) from exc
                if not isinstance(data, dict):
                    self.mark_degraded(f"{method} returned a non-object JSON-RPC envelope")
                    raise MCPSchemaViolation(
                        f"Server '{self._config.name}' returned a non-object JSON-RPC envelope for '{method}'"
                    )
                envelope = cast("dict[str, Any]", data)
                if "error" in envelope:
                    error = cast("dict[str, Any]", envelope["error"])
                    raise MCPClientError(
                        f"JSON-RPC error from '{self._config.name}': "
                        f"[{error.get('code', '?')}] {error.get('message', 'Unknown')}"
                    )

                result = envelope.get("result", {})
                if not isinstance(result, dict):
                    self.mark_degraded(f"{method} returned a non-object 'result' field")
                    raise MCPSchemaViolation(
                        f"Server '{self._config.name}' returned a non-object 'result' for '{method}'"
                    )
                return cast("dict[str, Any]", result)

            except MCPClientError:
                raise
            except httpx.ConnectError as exc:
                last_error = MCPConnectionError(
                    f"Cannot connect to MCP server '{self._config.name}' at {self._config.url}: {exc}"
                )
                if attempt < self._config.retry_limit - 1:
                    logger.warning(
                        "Connection attempt %d/%d to '%s' failed, retrying",
                        attempt + 1,
                        self._config.retry_limit,
                        self._config.name,
                    )
                    continue
            except httpx.TimeoutException as exc:
                last_error = MCPConnectionError(f"Timeout connecting to MCP server '{self._config.name}': {exc}")
                if attempt < self._config.retry_limit - 1:
                    continue
            except httpx.HTTPStatusError as exc:
                last_error = MCPClientError(
                    f"HTTP error from MCP server '{self._config.name}': {exc.response.status_code}"
                )
                if attempt < self._config.retry_limit - 1:
                    continue

        raise last_error or MCPConnectionError(
            f"Failed to connect to '{self._config.name}' after {self._config.retry_limit} attempts"
        )

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected).

        Notifications carry the same content-derived ``_meta`` as requests;
        no session header is attached (issue #2506).

        Args:
            method: JSON-RPC method name.
            params: Optional parameters.
        """
        params_with_meta: dict[str, Any] = dict(params) if params is not None else {}
        params_with_meta["_meta"] = self._next_request_meta(method, params)
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params_with_meta,
        }

        headers = {
            "Content-Type": _CONTENT_TYPE_JSON,
        } | self._build_auth_headers()

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._config.timeout_seconds)) as client:
                await client.post(
                    self._config.url,
                    json=payload,
                    headers=headers,
                )
        except Exception as exc:
            logger.warning(
                "Failed to send notification '%s' to '%s': %s",
                method,
                self._config.name,
                exc,
            )

    def _build_auth_headers(self) -> dict[str, str]:
        """Build authentication headers based on config.

        Returns:
            Dict of HTTP headers for authentication.
        """
        if self._config.auth_type == "bearer" and self._config.auth_token:
            return {"Authorization": f"Bearer {self._config.auth_token}"}
        if self._config.auth_type == "oauth" and self._config.auth_token:
            return {"Authorization": f"Bearer {self._config.auth_token}"}
        return {}


@dataclass(frozen=True)
class MCPManifestInfo:
    """Manifest digest and merge report produced at injection time (issue #3682).

    Carries the canonical merged tool set, its aggregate SHA-256 digest,
    per-server digests, name collisions discovered across servers, and an
    injection digest that lets callers detect discovery-to-injection drift.

    Attributes:
        aggregate_digest: SHA-256 of the canonical merged tool set.
        per_server_digests: Per-server SHA-256 digests keyed by server name.
        merged_tools: Canonical merged tool set as
            ``(server, name, description, input_schema)`` tuples.
        name_collisions: Pairs of ``(tool_name, [server, ...])`` where two or
            more servers advertise the same tool name.
        injection_digest: SHA-256 of the agent-config payload after injection,
            used to detect drift between discovery and injection.
    """

    aggregate_digest: str
    per_server_digests: dict[str, str]
    merged_tools: list[tuple[str, str, str, dict[str, Any]]]
    name_collisions: list[tuple[str, list[str]]]
    injection_digest: str


class MCPClientManager:
    """Manage connections to multiple remote MCP servers.

    Provides a unified interface for connecting to, discovering tools from,
    and calling tools on multiple remote MCP servers.

    Args:
        cost_meter: Optional per-server cost meter (issue #1673, AC4) shared
            across every session this manager opens. Metered tool calls are
            attributed per server per ``task_id``.
        metrics: Optional metrics collector that receives per-call latency /
            error and degraded-server availability events (AC5).
        task_id: Task identifier stamped onto cost-meter entries.
    """

    def __init__(
        self,
        *,
        cost_meter: MCPServerCostMeter | None = None,
        metrics: MCPMetricsCollector | None = None,
        task_id: str = "",
    ) -> None:
        self._sessions: dict[str, MCPClientSession] = {}
        self._cost_meter = cost_meter
        self._metrics = metrics
        self._task_id = task_id

    @property
    def sessions(self) -> dict[str, MCPClientSession]:
        """Active sessions by server name (copy)."""
        return self._sessions.copy()

    @property
    def cost_meter(self) -> MCPServerCostMeter | None:
        """The shared per-server cost meter, if wired in."""
        return self._cost_meter

    async def connect(self, config: RemoteServerConfig) -> MCPClientSession:
        """Connect to a remote MCP server.

        Creates a new session and initializes it. If a session with the same
        name already exists, it is closed first.

        Args:
            config: Server configuration.

        Returns:
            The connected session.

        Raises:
            MCPConnectionError: If the server cannot be reached.
            MCPAuthError: If authentication fails.
        """
        # Close existing session with same name
        if config.name in self._sessions:
            await self._sessions[config.name].close()

        session = MCPClientSession(
            config,
            cost_meter=self._cost_meter,
            metrics=self._metrics,
            task_id=self._task_id,
        )
        await session.connect()
        self._sessions[config.name] = session
        return session

    async def connect_all(self, configs: list[RemoteServerConfig]) -> list[MCPClientSession]:
        """Connect to multiple servers in parallel.

        Servers that fail to connect are logged as warnings but do not
        prevent other servers from connecting.

        Args:
            configs: List of server configurations.

        Returns:
            List of successfully connected sessions.
        """

        async def _try_connect(cfg: RemoteServerConfig) -> MCPClientSession | None:
            try:
                return await self.connect(cfg)
            except Exception as exc:
                logger.warning("Failed to connect to MCP server '%s': %s", cfg.name, exc)
                return None

        results = await asyncio.gather(
            *[_try_connect(cfg) for cfg in configs],
            return_exceptions=False,
        )
        return [s for s in results if s is not None]

    def get_session(self, name: str) -> MCPClientSession | None:
        """Get active session by server name.

        Args:
            name: Server name to look up.

        Returns:
            The session, or None if not connected.
        """
        return self._sessions.get(name)

    async def discover_all_tools(self) -> list[RemoteTool]:
        """Discover tools from all connected servers.

        Returns:
            Aggregated list of tools across all connected servers.
        """
        all_tools: list[RemoteTool] = []
        for session in self._sessions.values():
            if session.is_connected:
                tools = await session.list_tools()
                all_tools.extend(tools)
        return all_tools

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        cost_usd: float = 0.0,
        request_state: str | None = None,
    ) -> ToolCallResult:
        """Call a tool on a specific server.

        Args:
            server_name: Name of the server hosting the tool.
            tool_name: Name of the tool to call.
            arguments: Arguments to pass to the tool.
            cost_usd: Metered cost to attribute to this call.
            request_state: Opaque ``requestState`` echoed by a prior
                ``input_required`` result, when resuming that call.

        Returns:
            Result of the tool call.

        Raises:
            MCPClientError: If the server is not connected or the call fails.
        """
        session = self._require_session(server_name)
        return await session.call_tool(
            tool_name,
            arguments,
            cost_usd=cost_usd,
            request_state=request_state,
        )

    async def call_tool_streaming(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        stream_factory: Callable[[str | None, str], AsyncIterator[StreamChunk]],
        handle: StreamedToolCall | None = None,
        cost_usd: float = 0.0,
    ) -> ToolCallResult:
        """Stream a tool call on a specific server (issue #1673, AC2 + AC3).

        Delegates to :meth:`MCPClientSession.call_tool_streaming`.

        Raises:
            MCPClientError: If the server is not connected or the call fails.
        """
        session = self._require_session(server_name)
        return await session.call_tool_streaming(
            tool_name,
            arguments,
            stream_factory=stream_factory,
            handle=handle,
            cost_usd=cost_usd,
        )

    def _require_session(self, server_name: str) -> MCPClientSession:
        """Return the active session for ``server_name`` or raise."""
        session = self._sessions.get(server_name)
        if session is None:
            raise MCPClientError(
                f"No active session for server '{server_name}'. Connected: {sorted(self._sessions.keys())}"
            )
        return session

    def server_cost(self, server_name: str) -> float:
        """Return accumulated MCP spend for ``server_name`` on this task (AC4)."""
        if self._cost_meter is None:
            return 0.0
        return self._cost_meter.cost_for(self._task_id, server_name)

    def task_cost(self) -> float:
        """Return total MCP spend across all servers for this task (AC4)."""
        if self._cost_meter is None:
            return 0.0
        return self._cost_meter.task_total(self._task_id)

    def degraded_servers(self) -> list[str]:
        """Return names of servers currently marked degraded (AC5)."""
        return sorted(name for name, s in self._sessions.items() if s.is_degraded)

    async def close_all(self) -> None:
        """Close all active sessions."""
        for session in self._sessions.values():
            try:
                await session.close()
            except Exception as exc:
                logger.warning("Error closing session '%s': %s", session.server_name, exc)
        self._sessions.clear()

    @staticmethod
    def _compute_aggregate_digest(
        merged_tools: list[tuple[str, str, str, dict[str, Any]]],
    ) -> str:
        """Return SHA-256 digest of the canonical merged tool set.

        The canonical form is a JSON array of objects sorted by
        ``(server, name)`` to guarantee stable ordering across calls.
        """
        canonical = [
            {"server": server, "name": name, "description": desc, "inputSchema": schema}
            for server, name, desc, schema in sorted(merged_tools, key=lambda x: (x[0], x[1]))
        ]
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _compute_server_digest(
        server_name: str,
        tools: list[tuple[str, str, dict[str, Any]]],
    ) -> str:
        """Return SHA-256 digest of a single server's tool set.

        The canonical form is a JSON array of objects sorted by tool name.
        """
        canonical = [
            {"name": name, "description": desc, "inputSchema": schema}
            for name, desc, schema in sorted(tools, key=lambda x: x[0])
        ]
        return hashlib.sha256(
            json.dumps(
                {"server": server_name, "tools": canonical},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _compute_injection_digest(config: dict[str, Any]) -> str:
        """Return SHA-256 digest of the agent-config after MCP injection."""
        return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def inject_into_agent_config(
        self,
        agent_config: dict[str, Any],
        server_names: list[str] | None = None,
    ) -> tuple[dict[str, Any], MCPManifestInfo]:
        """Inject remote MCP server configs into agent spawn config (issue #3682).

        For Claude Code agents, adds entries to the ``mcpServers`` structure.
        For other agents, generates tool descriptions in the system prompt.

        Builds a canonical merged tool set across all targeted servers, computes
        SHA-256 digests (aggregate + per-server), detects name collisions
        across servers, and emits an :class:`MCPManifestInfo` so callers can
        detect discovery-to-injection drift.

        Args:
            agent_config: Agent configuration dict to augment.
            server_names: Subset of servers to include. Defaults to all.

        Returns:
            Tuple of ``(updated_agent_config, MCPManifestInfo)``.

        Raises:
            ValueError: If two or more targeted servers advertise the same
                tool name (name collision).
        """
        config = agent_config.copy()
        targets = server_names or list(self._sessions.keys())

        mcp_servers: dict[str, Any] = {}
        tool_descriptions: list[str] = []
        merged_tools: list[tuple[str, str, str, dict[str, Any]]] = []
        per_server_digests: dict[str, str] = {}
        name_owners: dict[str, list[str]] = {}

        for name in targets:
            session = self._sessions.get(name)
            if session is None or not session.is_connected:
                continue

            server_cfg = session._config

            entry: dict[str, Any] = {"url": server_cfg.url}
            if server_cfg.auth_type == "bearer" and server_cfg.auth_token:
                entry["headers"] = {"Authorization": f"Bearer {server_cfg.auth_token}"}
            mcp_servers[name] = entry

            for tool in session.tools:
                tool_descriptions.append(f"- {tool.name}: {tool.description} (server: {name})")
                schema = cast("dict[str, Any]", tool.input_schema) if isinstance(tool.input_schema, dict) else {}
                merged_tools.append((name, tool.name, tool.description, schema))
                name_owners.setdefault(tool.name, []).append(name)

        name_collisions: list[tuple[str, list[str]]] = sorted(
            (name, sorted(owners)) for name, owners in name_owners.items() if len(owners) > 1
        )
        if name_collisions:
            rendered = ", ".join(f"{n}={servers}" for n, servers in name_collisions)
            logger.error("MCP manifest name collision across servers: %s", rendered)
            raise ValueError(f"MCP name collision across servers: {rendered}")

        if mcp_servers:
            existing = config.get("mcp_config", {})
            if not isinstance(existing, dict):
                existing = {}
            existing_servers = existing.get("mcpServers", {})
            existing_servers.update(mcp_servers)
            existing["mcpServers"] = existing_servers
            config["mcp_config"] = existing

        if tool_descriptions:
            config["remote_tools_description"] = "\n".join(tool_descriptions)

        aggregate_digest = self._compute_aggregate_digest(merged_tools)

        for name, session in self._sessions.items():
            if name in targets and session.is_connected:
                server_tools = [(t.name, t.description, t.input_schema) for t in session.tools]
                per_server_digests[name] = self._compute_server_digest(name, server_tools)

        injection_digest = self._compute_injection_digest(config)

        manifest_info = MCPManifestInfo(
            aggregate_digest=aggregate_digest,
            per_server_digests=per_server_digests,
            merged_tools=merged_tools,
            name_collisions=name_collisions,
            injection_digest=injection_digest,
        )
        return config, manifest_info
