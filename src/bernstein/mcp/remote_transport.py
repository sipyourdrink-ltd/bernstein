"""Streamable HTTP transport for Bernstein MCP server.

Implements the MCP streamable HTTP transport spec for remote deployment.
Can be used with any ASGI server (uvicorn, Cloudflare Workers via Python worker).

Stateless serving (issue #2506): the transport keeps no per-client session
store. Every request is served from its body plus ``_meta`` alone, so any
transport instance can serve any request with no shared memory. Cross-call
continuity is anchored in the run journal and the audit chain (see
``anchor_stateless_call``), not in a session. A legacy client that still
sends the removed protocol-session header is accepted as a no-op behind the
compat shim until the shim's removal date, and never receives a session
header back.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import httpx

from bernstein.core.protocols.mcp.stateless_core import (
    LEGACY_SESSION_HEADER,
    REMOVAL_DATE,
    anchor_stateless_call,
    compat_shim_active,
    legacy_session_header_value,
    months_since_deprecation,
)
from bernstein.mcp.streaming import InFlightRegistry, cancelled_envelope

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

_DEFAULT_SERVER_URL = "http://127.0.0.1:8052"
_HTTP_TIMEOUT = 5.0

# JSON-RPC error codes per spec.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603
_CONTENT_TYPE_JSON = "application/json"

# Hostnames considered safe for listening without a configured auth token.
_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Env var names used to pick up the bearer auth token if not provided explicitly.
_TOKEN_ENV_VARS = ("BERNSTEIN_MCP_TOKEN", "BERNSTEIN_MCP_AUTH_TOKEN")

# Clear-text scheme, spelled as a bare token so a browser origin can only be
# built from it deliberately. Any origin carrying this scheme has to prove it
# is loopback-pinned (see ``RemoteMCPConfig.__post_init__``).
_PLAINTEXT_SCHEME = "http"

# Every scheme that carries traffic without TLS. `ws` and `ftp` are not valid
# browser origins, but enumerating them keeps the enforced policy identical to
# the documented one: clear-text is loopback-only, everything else is TLS.
# Non-URL CORS tokens such as `*` and `null` have no scheme and are untouched.
_CLEAR_TEXT_SCHEMES = frozenset({_PLAINTEXT_SCHEME, "ws", "ftp"})

# Default browser origin. Clear-text is acceptable here only because the origin
# is pinned to a loopback host, so the traffic never leaves the machine; every
# non-loopback origin is required to be TLS.
_DEFAULT_CORS_ORIGINS: tuple[str, ...] = (f"{_PLAINTEXT_SCHEME}://localhost:*",)


class RemoteMCPConfigError(RuntimeError):
    """Raised when an MCP remote transport config is unsafe to start with.

    Examples: binding a non-loopback host without a configured auth token, or
    explicitly setting auth_type='none' on a non-loopback host.
    """


def _resolve_token_from_env() -> str:
    """Return the first non-empty token found in the well-known env vars."""
    for name in _TOKEN_ENV_VARS:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def _is_localhost(host: str) -> bool:
    """Return True if ``host`` refers to the loopback interface only."""
    return host in _LOCALHOST_HOSTS


def _origin_host(origin: str) -> str | None:
    """Return the host of a CORS ``origin``, or ``None`` if it does not parse.

    Delegates to :func:`urllib.parse.urlsplit` so bracketed IPv6 literals are
    unwrapped by the stdlib instead of by hand. Hand-rolled bracket stripping
    silently accepted malformed authorities: ``http://[::1]evil.test`` and
    ``http://[::1]@evil.test`` both collapsed to ``::1`` and were admitted as
    loopback. ``urlsplit`` raises on those, so they are refused.

    ``hostname`` never parses the port, so the ``host:*`` port glob the server
    accepts survives, and the host comes back already lower-cased. Userinfo is
    refused outright: a browser ``Origin`` never carries it, so its presence
    means the value is not an origin.
    """
    try:
        parts = urlsplit(origin)
        if "@" in parts.netloc:
            return None
        return parts.hostname
    except ValueError:
        return None


def _is_plaintext_non_loopback_origin(origin: str) -> bool:
    """Return True if ``origin`` is clear-text and not pinned to loopback.

    Every clear-text scheme is covered, not just ``http``, so the policy the
    docs state holds for anything that would put a bearer token on the wire.
    An origin that does not parse is not provably loopback, so it is refused.
    """
    # Scheme is case-insensitive per RFC 3986.
    scheme, sep, _rest = origin.partition("://")
    if not sep or scheme.lower() not in _CLEAR_TEXT_SCHEMES:
        return False
    host = _origin_host(origin)
    return host is None or not _is_localhost(host)


def _constant_time_eq(left: str, right: str) -> bool:
    """Constant-time string compare that tolerates length differences."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _task_server_auth_headers() -> dict[str, str]:
    """Return task-server auth headers when a bearer token is configured."""
    token = os.environ.get("BERNSTEIN_AUTH_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


@dataclass(frozen=True)
class RemoteMCPConfig:
    """Configuration for remote MCP server transport.

    Safe-by-default: binds to localhost only and requires a bearer token.
    When constructed without an explicit ``auth_token`` the value is pulled
    from ``BERNSTEIN_MCP_TOKEN`` (or ``BERNSTEIN_MCP_AUTH_TOKEN``).

    Validation (in ``__post_init__``) refuses any combination that would
    expose MCP JSON-RPC without authentication:

    * ``auth_type='none'`` on a non-loopback host → :class:`RemoteMCPConfigError`
    * ``auth_type='bearer'`` with an empty token on a non-loopback host →
      :class:`RemoteMCPConfigError`
    * a clear-text (``http://``) CORS origin that is not pinned to a loopback
      host → :class:`RemoteMCPConfigError`
    """

    host: str = "127.0.0.1"
    port: int = 8053
    path: str = "/mcp"
    auth_type: str = "bearer"  # "none", "bearer", "oauth"
    auth_token: str = ""
    cors_origins: list[str] = field(default_factory=lambda: list(_DEFAULT_CORS_ORIGINS))

    def __post_init__(self) -> None:
        """Enforce safe-by-default policy and pick up env-provided tokens."""
        # Pull token from env when not explicitly provided. Use object.__setattr__
        # because the dataclass is frozen.
        if self.auth_type == "bearer" and not self.auth_token:
            env_token = _resolve_token_from_env()
            if env_token:
                object.__setattr__(self, "auth_token", env_token)

        localhost = _is_localhost(self.host)

        if self.auth_type == "none" and not localhost:
            msg = (
                f"Refusing to start MCP remote transport: host={self.host!r} is "
                "not loopback and auth_type='none'. Set auth_type='bearer' and "
                "provide a token via BERNSTEIN_MCP_TOKEN, or bind to 127.0.0.1."
            )
            raise RemoteMCPConfigError(msg)

        if self.auth_type == "bearer" and not self.auth_token and not localhost:
            msg = (
                f"Refusing to start MCP remote transport: host={self.host!r} is "
                "not loopback but no bearer token is configured. Set "
                "BERNSTEIN_MCP_TOKEN (or pass auth_token=...) before binding to "
                "a public interface."
            )
            raise RemoteMCPConfigError(msg)

        plaintext = [o for o in self.cors_origins if _is_plaintext_non_loopback_origin(o)]
        if plaintext:
            msg = (
                "Refusing to start MCP remote transport: clear-text CORS "
                f"origin(s) {plaintext!r} are not pinned to a loopback host. "
                "Bearer tokens travel on these origins, so use https:// or "
                "restrict the origin to 127.0.0.1, localhost or [::1]."
            )
            raise RemoteMCPConfigError(msg)


def _jsonrpc_error(
    code: int,
    message: str,
    req_id: int | str | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response."""
    resp: dict[str, Any] = {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
    }
    if req_id is not None:
        resp["id"] = req_id
    else:
        resp["id"] = None
    return resp


def _jsonrpc_result(
    result: Any,
    req_id: int | str | None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response."""
    return {
        "jsonrpc": "2.0",
        "result": result,
        "id": req_id,
    }


# -- Tool definitions (mirrors the FastMCP tools in server.py) ---------------

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "bernstein_health",
        "description": "Liveness check - always succeeds if the MCP server is running.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bernstein_run",
        "description": "Start an orchestration run by posting a task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "role": {"type": "string", "default": "backend"},
                "priority": {"type": "integer", "default": 2},
                "scope": {"type": "string", "default": "medium"},
                "complexity": {"type": "string", "default": "medium"},
                "estimated_minutes": {"type": "integer", "default": 30},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "bernstein_status",
        "description": "Return a summary of all task counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bernstein_tasks",
        "description": "List tasks with optional status filter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
            },
        },
    },
    {
        "name": "bernstein_cost",
        "description": "Return cost summary (total USD and per-role breakdown).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bernstein_stop",
        "description": "Graceful shutdown via SHUTDOWN signal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workdir": {"type": "string", "default": "."},
            },
        },
    },
    {
        "name": "bernstein_approve",
        "description": "Approve a pending/blocked task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "note": {"type": "string", "default": "Approved via MCP"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "bernstein_create_subtask",
        "description": "Create a subtask linked to a parent task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "parent_task_id": {"type": "string"},
                "goal": {"type": "string"},
                "role": {"type": "string", "default": "auto"},
                "priority": {"type": "integer", "default": 2},
                "scope": {"type": "string", "default": "medium"},
                "complexity": {"type": "string", "default": "medium"},
                "estimated_minutes": {"type": "integer"},
            },
            "required": ["parent_task_id", "goal"],
        },
    },
]

_SERVER_INFO: dict[str, Any] = {
    "name": "bernstein",
    "version": "1.0.0",
}

_CAPABILITIES: dict[str, Any] = {
    "tools": {"listChanged": False},
    # Server-side prompt templates surfaced via prompts/list and prompts/get.
    "prompts": {"listChanged": False},
    # The server honours notifications/cancelled for in-flight tool calls and
    # preserves partial output on cancel.
    "experimental": {"cancellation": {"partialResults": True}},
}


# Built-in prompt catalogue mirroring src/bernstein/mcp/prompts.py. Mirroring
# here keeps the streamable HTTP transport self-contained: it does not need
# to spin up a FastMCP instance to answer prompts/list and prompts/get.
_PROMPT_DEFS: list[dict[str, Any]] = [
    {
        "name": "orchestrate_goal",
        "description": "Plan a Bernstein orchestration run for a single goal.",
        "arguments": [
            {"name": "goal", "description": "What to accomplish.", "required": True},
            {"name": "role", "description": "Specialist role.", "required": False},
            {"name": "scope", "description": "Task scope.", "required": False},
        ],
    },
    {
        "name": "triage_failed_tasks",
        "description": "Triage the most recent failed tasks and propose next actions.",
        "arguments": [
            {"name": "limit", "description": "Max tasks to inspect.", "required": False},
        ],
    },
    {
        "name": "cost_recap",
        "description": "Summarise Bernstein cost by role for a stated window.",
        "arguments": [
            {"name": "window", "description": "Window label (e.g. today).", "required": False},
        ],
    },
]


class StreamableHTTPTransport:
    """MCP streamable HTTP transport implementation.

    Handles the HTTP request/response cycle for MCP messages using the
    streamable HTTP transport spec (POST for requests). No session store
    exists: every request is served from its body plus ``_meta`` alone, and
    served calls are anchored into the run journal / audit chain when those
    are wired in (issue #2506).

    Args:
        config: Transport configuration.
        server_url: Bernstein task server URL tool calls proxy to.
        journal: Optional run journal; every served ``tools/call`` becomes an
            ordered ``mcp.stateless_call`` journal entry.
        audit_chain: Optional audit chain store; requires ``journal``. Every
            served ``tools/call`` is anchored as an ``mcp.stateless_call``
            chain entry binding its content-derived ids to the journal head.
        today: Clock override for the legacy-session compat shim window
            (tests pin it; production uses the current date).
    """

    def __init__(
        self,
        config: RemoteMCPConfig,
        server_url: str = _DEFAULT_SERVER_URL,
        *,
        journal: EventJournal | None = None,
        audit_chain: AuditChainStore | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._config = config
        self._server_url = server_url
        self._journal = journal
        self._audit_chain = audit_chain
        self._today = today
        self._inflight = InFlightRegistry()

    def _legacy_session_shim_active(self) -> bool:
        """Whether the legacy protocol-session shim still accepts the header."""
        today = self._today() if self._today is not None else None
        return compat_shim_active("sessions", months_since_deprecation=months_since_deprecation(today))

    # -- public API ----------------------------------------------------------

    async def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        """Route incoming HTTP request to appropriate MCP handler.

        Args:
            method: HTTP method (GET, POST, DELETE).
            path: Request path.
            headers: HTTP headers (lower-cased keys).
            body: Raw request body.

        Returns:
            Tuple of (status_code, response_headers, response_body).
        """
        # OAuth-2 / OIDC discovery metadata. Served without authentication so
        # a client can locate the IdP before it has a token; standard well-known
        # paths only return content when ``BERNSTEIN_MCP_OAUTH_ISSUER`` is set,
        # otherwise 404 (anonymous/static-bearer remain the advertised path).
        if method == "GET" and self._is_well_known(path):
            return self._handle_well_known(path, headers)

        # Normalise path.
        if not path.rstrip("/").endswith(self._config.path.rstrip("/")):
            return (404, {"content-type": _CONTENT_TYPE_JSON}, b'{"error":"not found"}')

        # Auth check.
        if not self._authenticate(headers):
            return (
                401,
                {"content-type": _CONTENT_TYPE_JSON},
                b'{"error":"unauthorized"}',
            )

        # Legacy protocol-session shim: the removed header is accepted (and
        # ignored) for a bounded window, then refused with the removal date.
        if legacy_session_header_value(headers) is not None and not self._legacy_session_shim_active():
            message = (
                "protocol sessions were removed by the stateless MCP spec revision; "
                f"the {LEGACY_SESSION_HEADER} compatibility window ended on {REMOVAL_DATE.isoformat()}"
            )
            return (
                400,
                {"content-type": _CONTENT_TYPE_JSON},
                json.dumps({"error": message}).encode(),
            )

        if method == "POST":
            return await self._handle_post(body)
        if method == "GET":
            return self._handle_get()
        if method == "DELETE":
            return self._handle_delete()

        return (
            405,
            {"content-type": _CONTENT_TYPE_JSON, "allow": "GET, POST, OPTIONS"},
            b'{"error":"method not allowed"}',
        )

    # -- HTTP method handlers ------------------------------------------------

    async def _handle_post(
        self,
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle POST: JSON-RPC request/notification.

        The request is served from its body alone; no session is looked up
        or created, and no session header is returned.
        """
        try:
            message = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            err = _jsonrpc_error(_PARSE_ERROR, "Parse error")
            return (400, {"content-type": _CONTENT_TYPE_JSON}, json.dumps(err).encode())

        resp_headers: dict[str, str] = {"content-type": _CONTENT_TYPE_JSON}

        # Batch support.
        if isinstance(message, list):
            results: list[dict[str, Any]] = []
            for msg in cast("list[object]", message):
                if not isinstance(msg, dict):
                    result = _jsonrpc_error(_INVALID_REQUEST, "Invalid request")
                else:
                    result = await self._handle_jsonrpc(cast("dict[str, Any]", msg))
                if result is not None:
                    results.append(result)
            if not results:
                return (204, resp_headers, b"")
            return (200, resp_headers, json.dumps(results).encode())

        if not isinstance(message, dict):
            err = _jsonrpc_error(_INVALID_REQUEST, "Invalid request")
            return (200, resp_headers, json.dumps(err).encode())

        message_dict = cast("dict[str, Any]", message)
        result = await self._handle_jsonrpc(message_dict)
        if result is None:
            # Notification - no response.
            return (204, resp_headers, b"")
        return (200, resp_headers, json.dumps(result).encode())

    def _handle_get(self) -> tuple[int, dict[str, str], bytes]:
        """Handle GET: server-initiated SSE stream endpoint (stub - 501).

        Client-to-server streaming, cancellation, and partial-result
        preservation are handled over POST (``tools/call`` plus
        ``notifications/cancelled``). A server-initiated SSE push channel is
        not implemented, so GET still returns 501 with a pointer to POST.
        """
        return (
            501,
            {"content-type": _CONTENT_TYPE_JSON},
            b'{"error":"server-initiated SSE not implemented - use POST and notifications/cancelled"}',
        )

    def _handle_delete(self) -> tuple[int, dict[str, str], bytes]:
        """Handle DELETE: the removed session-close lifecycle.

        While the compat shim is active a legacy close request is
        acknowledged as a no-op (there is nothing to close); afterwards the
        method is gone. The shim-expiry path for a request that still
        carries the legacy header is handled in :meth:`handle_request`.
        """
        if self._legacy_session_shim_active():
            return (200, {"content-type": _CONTENT_TYPE_JSON}, b'{"status":"ok"}')
        return (
            405,
            {"content-type": _CONTENT_TYPE_JSON, "allow": "GET, POST, OPTIONS"},
            b'{"error":"method not allowed"}',
        )

    # -- JSON-RPC dispatch ---------------------------------------------------

    async def _handle_jsonrpc(
        self,
        message: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Process a single JSON-RPC message from its body alone.

        Args:
            message: Parsed JSON-RPC message.

        Returns:
            JSON-RPC response dict, or None for notifications.
        """
        if message.get("jsonrpc") != "2.0":
            return _jsonrpc_error(_INVALID_REQUEST, "Invalid JSON-RPC version")

        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params", {})

        # Notifications have no id - fire and forget.
        is_notification = req_id is None and "id" not in message

        handler = self._get_method_handler(method)
        if handler is None:
            if is_notification:
                return None
            return _jsonrpc_error(_METHOD_NOT_FOUND, f"Method not found: {method}", req_id)

        try:
            # tools/call needs its JSON-RPC id so the call can be tracked for
            # cancellation; other handlers take only (params).
            if method == "tools/call":
                result = await self._method_tools_call(params, req_id)
            else:
                result = await handler(params)
        except Exception as exc:
            logger.exception("Error handling method %s", method)
            if is_notification:
                return None
            return _jsonrpc_error(_INTERNAL_ERROR, str(exc), req_id)

        if is_notification:
            return None
        return _jsonrpc_result(result, req_id)

    def _anchor_served_call(self, method: str, params: dict[str, Any]) -> None:
        """Anchor a served call into the run journal and audit chain.

        The journal row and chain entry replace the deleted session store as
        the continuity record: ordering reconstructs from chain entries
        alone (issue #2506). Anchoring failures are logged, not raised, so a
        full audit volume cannot take request serving down; the gap is still
        visible to a verifier as a missing call index.
        """
        if self._journal is None:
            return
        try:
            anchor_stateless_call(
                journal=self._journal,
                method=method,
                params=params,
                chain=self._audit_chain,
            )
        except Exception:
            logger.exception("Failed to anchor mcp.stateless_call for %s", method)

    def _get_method_handler(self, method: str | None) -> Any:
        """Look up handler for a JSON-RPC method name."""
        handlers: dict[str, Any] = {
            "initialize": self._method_initialize,
            "tools/list": self._method_tools_list,
            "tools/call": self._method_tools_call,
            "prompts/list": self._method_prompts_list,
            "prompts/get": self._method_prompts_get,
            "ping": self._method_ping,
            "notifications/initialized": self._method_noop,
            "notifications/cancelled": self._method_cancelled,
        }
        return handlers.get(method or "")

    # -- MCP method implementations ------------------------------------------

    async def _method_initialize(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'initialize' - return server info and capabilities.

        Retained for clients that still send the legacy handshake; the
        result carries no session identity. Alongside the static spec
        ``capabilities`` object, the result carries a runtime
        ``capabilityCard`` describing the live transports, auth modes,
        active tool tier, and cost-meter state so a client can decide how to
        connect without trial and error.
        """
        from bernstein.mcp.capability import build_capability_card

        _ = params
        return {
            "protocolVersion": "2025-03-26",
            "serverInfo": _SERVER_INFO,
            "capabilities": _CAPABILITIES,
            "capabilityCard": build_capability_card(),
        }

    async def _method_tools_list(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'tools/list' - return available tools."""
        _ = params
        return {"tools": _TOOL_DEFS}

    async def _method_tools_call(
        self,
        params: dict[str, Any],
        req_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Handle 'tools/call' - execute a tool and return result.

        The tool runs inside a cancellable task tracked by its JSON-RPC id, so
        a ``notifications/cancelled`` for that id stops the work and the
        accumulated partial output is returned (``cancelled: true`` with the
        preserved ``partial`` chunks) rather than discarded.

        The tool's JSON payload is wrapped in the per-call cost-meter envelope
        (latency, cost, trace id, status) so the remote transport emits the
        same observable shape as the stdio/SSE server. The envelope is a no-op
        when the meter is disabled via ``BERNSTEIN_MCP_COST_METER``.

        When a journal / audit chain is wired in, the served call is
        anchored as an ``mcp.stateless_call`` entry before execution, so the
        chain records the call even if the tool itself fails (issue #2506).
        """
        from bernstein.mcp.cost_meter import measure_call, wrap_envelope

        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        self._anchor_served_call("tools/call", params)

        # Untracked calls (no id) still execute, just without cancel support.
        call = await self._inflight.register(req_id, tool_name) if req_id is not None else None

        meter_ctx = measure_call(tool_name)
        meter = meter_ctx.__enter__()
        meter_finalised = False
        text = ""
        try:
            if call is not None:
                assert req_id is not None
                # Seed a partial chunk so a cancel mid-flight preserves the
                # in-progress context rather than returning an empty result.
                call.append_partial(json.dumps({"status": "running", "tool": tool_name}))
                task: asyncio.Task[str] = asyncio.create_task(self._execute_tool(tool_name, arguments))
                await self._inflight.attach_task(req_id, task)
                task_result = (await asyncio.gather(task, return_exceptions=True))[0]
                if isinstance(task_result, asyncio.CancelledError):
                    current = await self._inflight.get(req_id)
                    if current is not None and current.cancelled:
                        meter_ctx.__exit__(None, None, None)
                        meter_finalised = True
                        return cancelled_envelope(current, meter.to_dict())
                    raise task_result
                if isinstance(task_result, BaseException):
                    raise task_result
                text = task_result
            else:
                text = await self._execute_tool(tool_name, arguments)
        except Exception as exc:
            # Finalise the meter for the failed call, then emit it alongside
            # the error so observability covers failures too.
            with suppress(Exception):
                meter_ctx.__exit__(type(exc), exc, exc.__traceback__)
            meter_finalised = True
            logger.warning("Tool %s failed: %s", tool_name, exc)
            error_payload = wrap_envelope(json.dumps({"error": str(exc)}), meter)
            return {
                "content": [{"type": "text", "text": error_payload}],
                "isError": True,
            }
        finally:
            if req_id is not None:
                await self._inflight.discard(req_id)
            if not meter_finalised:
                with suppress(Exception):
                    meter_ctx.__exit__(None, None, None)
        return {
            "content": [{"type": "text", "text": wrap_envelope(text, meter)}],
        }

    async def _method_prompts_list(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'prompts/list' - return the built-in prompt catalogue.

        Common auto-discovery hosts probe this surface to populate a prompt
        picker. The catalogue is the same one the FastMCP server registers,
        kept in sync via ``_PROMPT_DEFS`` on this transport.
        """
        _ = params
        return {"prompts": _PROMPT_DEFS}

    async def _method_prompts_get(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'prompts/get' - render a named prompt with arguments.

        Args:
            params: JSON-RPC params carrying ``name`` and optional ``arguments``.

        Returns:
            A prompt response with a single user-role text message.

        Raises:
            ValueError: When the requested prompt name is unknown.
        """
        from bernstein.mcp import prompts

        prompt_attrs = vars(prompts)
        orchestrate_goal_template = cast(
            "Callable[[str, str, str], str]",
            prompt_attrs["_orchestrate_goal_template"],
        )
        triage_failed_tasks_template = cast(
            "Callable[[int], str]",
            prompt_attrs["_triage_failed_tasks_template"],
        )
        cost_recap_template = cast(
            "Callable[[str], str]",
            prompt_attrs["_cost_recap_template"],
        )

        name_raw = params.get("name", "")
        name = name_raw if isinstance(name_raw, str) else ""
        arguments_raw = params.get("arguments", {})
        arguments = cast("dict[str, object]", arguments_raw) if isinstance(arguments_raw, dict) else {}
        if name == "orchestrate_goal":
            goal = arguments.get("goal", "")
            role = arguments.get("role", "backend")
            scope = arguments.get("scope", "medium")
            body = orchestrate_goal_template(
                goal if isinstance(goal, str) else "",
                role if isinstance(role, str) else "backend",
                scope if isinstance(scope, str) else "medium",
            )
        elif name == "triage_failed_tasks":
            limit_raw = arguments.get("limit", 5)
            if isinstance(limit_raw, int):
                limit = limit_raw
            elif isinstance(limit_raw, str):
                try:
                    limit = int(limit_raw)
                except ValueError:
                    limit = 5
            else:
                limit = 5
            body = triage_failed_tasks_template(limit)
        elif name == "cost_recap":
            window = arguments.get("window", "today")
            body = cost_recap_template(window if isinstance(window, str) else "today")
        else:
            msg = f"Unknown prompt: {name}"
            raise ValueError(msg)
        return {
            "description": next((p["description"] for p in _PROMPT_DEFS if p["name"] == name), ""),
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": body},
                }
            ],
        }

    async def _method_cancelled(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'notifications/cancelled' - stop an in-flight tool call.

        Per the MCP spec the notification carries ``requestId`` (the id of the
        ``tools/call`` to cancel). Cancelling marks the tracked call and
        cancels its task; the originating ``tools/call`` handler then returns
        the preserved partial output. Cancelling an unknown or already-settled
        id is a no-op, as the spec requires.
        """
        request_id = params.get("requestId")
        if request_id is not None:
            await self._inflight.cancel(request_id)
        return {}

    async def _method_ping(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'ping' - return empty result."""
        _ = params
        return {}

    async def _method_noop(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle notifications that need no response."""
        _ = params
        return {}

    # -- Tool execution (proxies to Bernstein task server) --------------------

    async def _execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a Bernstein tool by proxying to the task server.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            JSON string result.

        Raises:
            ValueError: If the tool name is unknown.
        """
        if name == "bernstein_health":
            return json.dumps({"status": "ok"})

        if name == "bernstein_status":
            return await self._proxy_get("/status")

        if name == "bernstein_tasks":
            params: dict[str, str] = {}
            if arguments.get("status"):
                params["status"] = arguments["status"]
            return await self._proxy_get("/tasks", params=params)

        if name == "bernstein_cost":
            raw = await self._proxy_get("/status")
            data = json.loads(raw)
            per_role_raw: list[dict[str, Any]] = data.get("per_role", [])
            return json.dumps(
                {
                    "total_cost_usd": data.get("total_cost_usd", 0.0),
                    "per_role": [{"role": r["role"], "cost_usd": r.get("cost_usd", 0.0)} for r in per_role_raw],
                }
            )

        if name == "bernstein_run":
            goal = arguments.get("goal", "")
            payload: dict[str, Any] = {
                "title": goal[:120],
                "description": goal,
                "role": arguments.get("role", "backend"),
                "priority": arguments.get("priority", 2),
                "scope": arguments.get("scope", "medium"),
                "complexity": arguments.get("complexity", "medium"),
                "estimated_minutes": arguments.get("estimated_minutes", 30),
            }
            return await self._proxy_post("/tasks", payload)

        if name == "bernstein_stop":
            from pathlib import Path

            workdir = arguments.get("workdir", ".")
            signals_dir = Path(workdir) / ".sdd" / "runtime" / "signals"
            signals_dir.mkdir(parents=True, exist_ok=True)
            shutdown_file = signals_dir / "SHUTDOWN"
            shutdown_file.write_text("mcp-remote-stop\n", encoding="utf-8")
            return json.dumps({"status": "shutdown signal sent", "path": str(shutdown_file)})

        if name == "bernstein_approve":
            task_id = arguments["task_id"]
            note = arguments.get("note", "Approved via MCP")
            return await self._proxy_post(
                f"/tasks/{task_id}/complete",
                {"result_summary": note},
            )

        if name == "bernstein_create_subtask":
            payload_sub: dict[str, Any] = {
                "parent_task_id": arguments["parent_task_id"],
                "title": arguments["goal"][:120],
                "description": arguments["goal"],
                "role": arguments.get("role", "auto"),
                "priority": arguments.get("priority", 2),
                "scope": arguments.get("scope", "medium"),
                "complexity": arguments.get("complexity", "medium"),
            }
            if arguments.get("estimated_minutes") is not None:
                payload_sub["estimated_minutes"] = arguments["estimated_minutes"]
            return await self._proxy_post("/tasks/self-create", payload_sub)

        msg = f"Unknown tool: {name}"
        raise ValueError(msg)

    async def _proxy_get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> str:
        """GET request to Bernstein task server."""
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{self._server_url}{path}",
                params=params,
                headers=_task_server_auth_headers(),
            )
            resp.raise_for_status()
            return resp.text

    async def _proxy_post(self, path: str, payload: dict[str, Any]) -> str:
        """POST request to Bernstein task server."""
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{self._server_url}{path}",
                json=payload,
                headers=_task_server_auth_headers(),
            )
            resp.raise_for_status()
            return resp.text

    # -- OAuth discovery -----------------------------------------------------

    @staticmethod
    def _is_well_known(path: str) -> bool:
        """Return True for the protected-resource discovery path.

        Bernstein only publishes RFC 9728 protected-resource metadata; the
        RFC 8414 authorization-server metadata is owned by the IdP itself.
        """
        from bernstein.mcp.oauth import PR_METADATA_PATH

        return path == PR_METADATA_PATH

    def _handle_well_known(
        self,
        path: str,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, str], bytes]:
        """Serve protected-resource metadata (RFC 9728 / MCP draft).

        Returns:
            (200, headers, json-body) when discovery is enabled; 404 otherwise.
        """
        from bernstein.mcp.oauth import (
            PR_METADATA_PATH,
            protected_resource_metadata,
        )

        if path == PR_METADATA_PATH:
            # Build the absolute resource URL from the Host header so the
            # advertised resource matches what the client called.
            host = headers.get("host", f"{self._config.host}:{self._config.port}")
            scheme = headers.get("x-forwarded-proto", "http")
            resource_url = f"{scheme}://{host}{self._config.path}"
            meta = protected_resource_metadata(resource_url)
        else:
            meta = None

        if meta is None:
            return (
                404,
                {"content-type": _CONTENT_TYPE_JSON},
                b'{"error":"oauth discovery not configured"}',
            )
        return (
            200,
            {"content-type": _CONTENT_TYPE_JSON, "cache-control": "public, max-age=300"},
            json.dumps(meta).encode(),
        )

    # -- Auth ----------------------------------------------------------------

    def _authenticate(self, headers: dict[str, str]) -> bool:
        """Validate authentication credentials.

        Args:
            headers: HTTP request headers (lower-cased keys).

        Returns:
            True if the request is authenticated.
        """
        if self._config.auth_type == "none":
            return True

        if self._config.auth_type == "bearer":
            expected = self._config.auth_token
            if not expected:
                # Defence in depth: never treat a blank token as valid even
                # when callers have (incorrectly) reached this branch on a
                # localhost-only bind.
                return False
            auth_header = headers.get("authorization", "")
            if not auth_header.startswith("Bearer "):
                return False
            token = auth_header[7:]
            return _constant_time_eq(token, expected)

        # Unknown auth type - deny.
        return False


def create_asgi_app(
    server_url: str = _DEFAULT_SERVER_URL,
    config: RemoteMCPConfig | None = None,
    *,
    journal: EventJournal | None = None,
    audit_chain: AuditChainStore | None = None,
) -> Any:
    """Create ASGI application wrapping Bernstein MCP server with streamable HTTP transport.

    Args:
        server_url: Bernstein task server URL.
        config: Transport configuration. Uses defaults if None.
        journal: Optional run journal; served ``tools/call`` requests become
            ordered ``mcp.stateless_call`` journal entries.
        audit_chain: Optional audit chain store anchoring every served call.

    Returns:
        ASGI application callable.
    """
    cfg = config or RemoteMCPConfig()
    transport = StreamableHTTPTransport(
        config=cfg,
        server_url=server_url,
        journal=journal,
        audit_chain=audit_chain,
    )

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        """ASGI application entry point."""
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        if scope["type"] != "http":
            return

        # Read request body.
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break

        method = scope["method"]
        path = scope["path"]
        raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers}

        # CORS preflight.
        if method == "OPTIONS":
            cors_headers = _cors_headers(cfg)
            await _send_response(send, 204, cors_headers, b"")
            return

        status, resp_headers, resp_body = await transport.handle_request(method, path, headers, body)
        resp_headers.update(_cors_headers(cfg))
        await _send_response(send, status, resp_headers, resp_body)

    return app


def _cors_headers(config: RemoteMCPConfig) -> dict[str, str]:
    """Build CORS response headers.

    The legacy protocol-session header stays preflight-allowed so browsers
    running older clients can still send it during the compat window (the
    transport ignores it); it is never exposed because no response carries
    it.
    """
    origin = ", ".join(config.cors_origins)
    return {
        "access-control-allow-origin": origin,
        "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
        "access-control-allow-headers": f"content-type, authorization, {LEGACY_SESSION_HEADER}",
    }


async def _send_response(
    send: Any,
    status: int,
    headers: dict[str, str],
    body: bytes,
) -> None:
    """Send an ASGI HTTP response."""
    raw_headers = [(k.encode(), v.encode()) for k, v in headers.items()]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": raw_headers,
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
        }
    )


def run_remote(
    server_url: str = _DEFAULT_SERVER_URL,
    host: str = "127.0.0.1",
    port: int = 8053,
    auth_token: str | None = None,
) -> None:
    """Start MCP server with streamable HTTP transport for remote access.

    Args:
        server_url: Bernstein task server URL to proxy tool calls to.
        host: Host to bind to. Defaults to loopback; binding to ``0.0.0.0``
            requires a bearer token (passed via ``auth_token`` or the
            ``BERNSTEIN_MCP_TOKEN`` env var), otherwise a
            :class:`RemoteMCPConfigError` is raised at startup.
        port: Port to bind to.
        auth_token: Explicit bearer token. Falls back to
            ``BERNSTEIN_MCP_TOKEN`` / ``BERNSTEIN_MCP_AUTH_TOKEN`` env vars.

    Raises:
        RemoteMCPConfigError: When the host/token combination would expose
            the MCP endpoint without authentication.
    """
    import uvicorn

    token = auth_token if auth_token is not None else _resolve_token_from_env()
    config = RemoteMCPConfig(host=host, port=port, auth_token=token)
    app = create_asgi_app(server_url=server_url, config=config)
    uvicorn.run(app, host=host, port=port)
