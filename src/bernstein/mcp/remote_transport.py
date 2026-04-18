"""Streamable HTTP transport for Bernstein MCP server.

Implements the MCP streamable HTTP transport spec for remote deployment.
Can be used with any ASGI server (uvicorn, Cloudflare Workers via Python worker).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

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


def _constant_time_eq(left: str, right: str) -> bool:
    """Constant-time string compare that tolerates length differences."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


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
    """

    host: str = "127.0.0.1"
    port: int = 8053
    path: str = "/mcp"
    auth_type: str = "bearer"  # "none", "bearer", "oauth"
    auth_token: str = ""
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:*"])
    max_sessions: int = 100
    session_timeout_seconds: int = 3600

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


@dataclass
class MCPSession:
    """Per-client MCP session state."""

    session_id: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    tools_listed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


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
        "description": "Liveness check — always succeeds if the MCP server is running.",
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
}


class StreamableHTTPTransport:
    """MCP streamable HTTP transport implementation.

    Handles the HTTP request/response cycle for MCP messages using
    the streamable HTTP transport spec (POST for requests, GET for SSE
    streams, DELETE to close sessions).
    """

    def __init__(
        self,
        config: RemoteMCPConfig,
        server_url: str = _DEFAULT_SERVER_URL,
    ) -> None:
        self._config = config
        self._server_url = server_url
        self._sessions: dict[str, MCPSession] = {}
        self._lock = asyncio.Lock()

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

        if method == "POST":
            return await self._handle_post(headers, body)
        if method == "GET":
            return self._handle_get(headers)
        if method == "DELETE":
            return await self._handle_delete(headers)

        return (
            405,
            {"content-type": _CONTENT_TYPE_JSON, "allow": "GET, POST, DELETE"},
            b'{"error":"method not allowed"}',
        )

    # -- HTTP method handlers ------------------------------------------------

    async def _handle_post(
        self,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle POST: JSON-RPC request/notification."""
        try:
            message = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            err = _jsonrpc_error(_PARSE_ERROR, "Parse error")
            return (400, {"content-type": _CONTENT_TYPE_JSON}, json.dumps(err).encode())

        session_id = headers.get("mcp-session-id")
        session = await self._get_or_create_session(session_id)
        resp_headers: dict[str, str] = {
            "content-type": _CONTENT_TYPE_JSON,
            "mcp-session-id": session.session_id,
        }

        # Batch support.
        if isinstance(message, list):
            results: list[dict[str, Any]] = []
            for msg in message:
                result = await self._handle_jsonrpc(session, msg)
                if result is not None:
                    results.append(result)
            if not results:
                return (204, resp_headers, b"")
            return (200, resp_headers, json.dumps(results).encode())

        result = await self._handle_jsonrpc(session, message)
        if result is None:
            # Notification — no response.
            return (204, resp_headers, b"")
        return (200, resp_headers, json.dumps(result).encode())

    def _handle_get(
        self,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle GET: SSE stream endpoint (stub — returns 501)."""
        session_id = headers.get("mcp-session-id")
        if session_id and session_id not in self._sessions:
            return (
                404,
                {"content-type": _CONTENT_TYPE_JSON},
                b'{"error":"session not found"}',
            )
        # Server-initiated SSE not yet implemented.
        return (
            501,
            {"content-type": _CONTENT_TYPE_JSON},
            b'{"error":"SSE stream not implemented - use POST for request/response"}',
        )

    async def _handle_delete(
        self,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle DELETE: close session."""
        session_id = headers.get("mcp-session-id")
        if not session_id or session_id not in self._sessions:
            return (
                404,
                {"content-type": _CONTENT_TYPE_JSON},
                b'{"error":"session not found"}',
            )
        async with self._lock:
            del self._sessions[session_id]
        return (200, {"content-type": _CONTENT_TYPE_JSON}, b'{"status":"session closed"}')

    # -- JSON-RPC dispatch ---------------------------------------------------

    async def _handle_jsonrpc(
        self,
        session: MCPSession,
        message: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Process a single JSON-RPC message.

        Args:
            session: Current MCP session.
            message: Parsed JSON-RPC message.

        Returns:
            JSON-RPC response dict, or None for notifications.
        """
        session.last_active = time.time()

        if message.get("jsonrpc") != "2.0":
            return _jsonrpc_error(_INVALID_REQUEST, "Invalid JSON-RPC version")

        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params", {})

        # Notifications have no id — fire and forget.
        is_notification = req_id is None and "id" not in message

        handler = self._get_method_handler(method)
        if handler is None:
            if is_notification:
                return None
            return _jsonrpc_error(_METHOD_NOT_FOUND, f"Method not found: {method}", req_id)

        try:
            result = await handler(session, params)
        except Exception as exc:
            logger.exception("Error handling method %s", method)
            if is_notification:
                return None
            return _jsonrpc_error(_INTERNAL_ERROR, str(exc), req_id)

        if is_notification:
            return None
        return _jsonrpc_result(result, req_id)

    def _get_method_handler(self, method: str | None) -> Any:
        """Look up handler for a JSON-RPC method name."""
        handlers: dict[str, Any] = {
            "initialize": self._method_initialize,
            "tools/list": self._method_tools_list,
            "tools/call": self._method_tools_call,
            "ping": self._method_ping,
            "notifications/initialized": self._method_noop,
        }
        return handlers.get(method or "")

    # -- MCP method implementations ------------------------------------------

    async def _method_initialize(
        self,
        session: MCPSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'initialize' — return server info and capabilities."""
        session.metadata["client_info"] = params.get("clientInfo", {})
        return {
            "protocolVersion": "2025-03-26",
            "serverInfo": _SERVER_INFO,
            "capabilities": _CAPABILITIES,
        }

    async def _method_tools_list(
        self,
        session: MCPSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'tools/list' — return available tools."""
        session.tools_listed = True
        return {"tools": _TOOL_DEFS}

    async def _method_tools_call(
        self,
        session: MCPSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'tools/call' — execute a tool and return result."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        try:
            text = await self._execute_tool(tool_name, arguments)
        except Exception as exc:
            logger.warning("Tool %s failed: %s", tool_name, exc)
            return {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                "isError": True,
            }

        return {
            "content": [{"type": "text", "text": text}],
        }

    async def _method_ping(
        self,
        session: MCPSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'ping' — return empty result."""
        return {}

    async def _method_noop(
        self,
        session: MCPSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle notifications that need no response."""
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
        from bernstein.mcp.server import _auth_headers

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{self._server_url}{path}",
                params=params,
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            return resp.text

    async def _proxy_post(self, path: str, payload: dict[str, Any]) -> str:
        """POST request to Bernstein task server."""
        from bernstein.mcp.server import _auth_headers

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{self._server_url}{path}",
                json=payload,
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            return resp.text

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

        # Unknown auth type — deny.
        return False

    # -- Session management --------------------------------------------------

    async def _get_or_create_session(self, session_id: str | None) -> MCPSession:
        """Get existing session or create new one.

        Args:
            session_id: Existing session ID from headers, or None.

        Returns:
            Active MCPSession.

        Raises:
            ValueError: If max sessions exceeded.
        """
        async with self._lock:
            # Prune expired sessions.
            now = time.time()
            expired = [
                sid for sid, s in self._sessions.items() if now - s.last_active > self._config.session_timeout_seconds
            ]
            for sid in expired:
                del self._sessions[sid]

            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
                session.last_active = now
                return session

            if len(self._sessions) >= self._config.max_sessions:
                msg = "Max sessions exceeded"
                raise ValueError(msg)

            new_id = str(uuid.uuid4())
            session = MCPSession(session_id=new_id)
            self._sessions[new_id] = session
            return session


def create_asgi_app(
    server_url: str = _DEFAULT_SERVER_URL,
    config: RemoteMCPConfig | None = None,
) -> Any:
    """Create ASGI application wrapping Bernstein MCP server with streamable HTTP transport.

    Args:
        server_url: Bernstein task server URL.
        config: Transport configuration. Uses defaults if None.

    Returns:
        ASGI application callable.
    """
    cfg = config or RemoteMCPConfig()
    transport = StreamableHTTPTransport(config=cfg, server_url=server_url)

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
    """Build CORS response headers."""
    origin = ", ".join(config.cors_origins)
    return {
        "access-control-allow-origin": origin,
        "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
        "access-control-allow-headers": "content-type, authorization, mcp-session-id",
        "access-control-expose-headers": "mcp-session-id",
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
