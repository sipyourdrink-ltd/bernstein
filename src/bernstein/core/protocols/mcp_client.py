"""MCP client for consuming remote MCP tools.

Connects to remote MCP servers via streamable HTTP transport,
discovers available tools, and calls them on behalf of Bernstein agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteServerConfig:
    """Configuration for a remote MCP server connection.

    Attributes:
        name: Human-readable identifier for the server.
        url: Base URL of the remote MCP server.
        transport: Transport type — ``"streamable-http"`` or ``"sse"``.
        auth_type: Authentication method — ``"none"``, ``"bearer"``, or ``"oauth"``.
        auth_token: Bearer token when auth_type is ``"bearer"``.
        oauth_client_id: OAuth client ID when auth_type is ``"oauth"``.
        oauth_client_secret: OAuth client secret when auth_type is ``"oauth"``.
        timeout_seconds: Request timeout in seconds.
        retry_limit: Maximum number of retries for failed requests.
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


class MCPClientSession:
    """Active session with a remote MCP server.

    Handles the JSON-RPC 2.0 protocol over HTTP, including initialization,
    tool discovery, and tool invocation.

    Args:
        config: Configuration for the remote server.
    """

    def __init__(self, config: RemoteServerConfig) -> None:
        self._config = config
        self._session_id: str = str(uuid.uuid4())
        self._mcp_session_id: str | None = None
        self._tools: list[RemoteTool] = []
        self._initialized: bool = False
        self._request_id: int = 0

    @property
    def server_name(self) -> str:
        """Name of the connected server."""
        return self._config.name

    @property
    def tools(self) -> list[RemoteTool]:
        """List of discovered tools (copy)."""
        return list(self._tools)

    @property
    def is_connected(self) -> bool:
        """Whether the session has been initialized."""
        return self._initialized

    async def connect(self) -> None:
        """Initialize MCP session with remote server.

        Sends the ``initialize`` request followed by an ``initialized``
        notification, then discovers available tools.

        Raises:
            MCPConnectionError: If the server cannot be reached.
            MCPAuthError: If authentication fails.
        """
        # Send initialize request
        init_result = await self._send_jsonrpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "bernstein",
                    "version": "1.0.0",
                },
            },
        )

        # Store session ID from response headers if provided
        logger.info(
            "MCP session initialized with server '%s': %s",
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

        self._tools = []
        for tool_data in raw_tools:
            tool = RemoteTool(
                name=tool_data.get("name", ""),
                description=tool_data.get("description", ""),
                server_name=self._config.name,
                input_schema=tool_data.get("inputSchema", {}),
            )
            self._tools.append(tool)

        logger.info(
            "Discovered %d tools from server '%s'",
            len(self._tools),
            self._config.name,
        )
        return list(self._tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolCallResult:
        """Call a tool on the remote server.

        Args:
            tool_name: Name of the tool to call.
            arguments: Arguments to pass to the tool.

        Returns:
            Result of the tool call.

        Raises:
            MCPToolNotFoundError: If the tool is not found on this server.
            MCPClientError: If the call fails.
        """
        # Verify tool exists
        known_names = {t.name for t in self._tools}
        if known_names and tool_name not in known_names:
            raise MCPToolNotFoundError(
                f"Tool '{tool_name}' not found on server '{self._config.name}'. Available: {sorted(known_names)}"
            )

        result = await self._send_jsonrpc(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )

        # Parse MCP tool result content
        content_parts = result.get("content", [])
        text_parts: list[str] = []
        for part in content_parts:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))

        return ToolCallResult(
            content="\n".join(text_parts) if text_parts else json.dumps(result),
            is_error=bool(result.get("isError", False)),
            metadata={"server": self._config.name, "tool": tool_name},
        )

    async def close(self) -> None:
        """Close the MCP session."""
        self._initialized = False
        self._tools = []
        logger.info("Closed MCP session with server '%s'", self._config.name)

    async def _send_jsonrpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send JSON-RPC request to remote server.

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
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self._build_auth_headers(),
        }

        if self._mcp_session_id is not None:
            headers["Mcp-Session-Id"] = self._mcp_session_id

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

                # Capture session ID from response header
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self._mcp_session_id = session_id

                data = response.json()
                if "error" in data:
                    error = data["error"]
                    raise MCPClientError(
                        f"JSON-RPC error from '{self._config.name}': "
                        f"[{error.get('code', '?')}] {error.get('message', 'Unknown')}"
                    )

                return dict(data.get("result", {}))

            except (MCPAuthError, MCPClientError):
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

        Args:
            method: JSON-RPC method name.
            params: Optional parameters.
        """
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        headers = {
            "Content-Type": "application/json",
            **self._build_auth_headers(),
        }
        if self._mcp_session_id is not None:
            headers["Mcp-Session-Id"] = self._mcp_session_id

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


class MCPClientManager:
    """Manage connections to multiple remote MCP servers.

    Provides a unified interface for connecting to, discovering tools from,
    and calling tools on multiple remote MCP servers.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, MCPClientSession] = {}

    @property
    def sessions(self) -> dict[str, MCPClientSession]:
        """Active sessions by server name (copy)."""
        return dict(self._sessions)

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

        session = MCPClientSession(config)
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

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolCallResult:
        """Call a tool on a specific server.

        Args:
            server_name: Name of the server hosting the tool.
            tool_name: Name of the tool to call.
            arguments: Arguments to pass to the tool.

        Returns:
            Result of the tool call.

        Raises:
            MCPClientError: If the server is not connected or the call fails.
        """
        session = self._sessions.get(server_name)
        if session is None:
            raise MCPClientError(
                f"No active session for server '{server_name}'. Connected: {sorted(self._sessions.keys())}"
            )
        return await session.call_tool(tool_name, arguments)

    async def close_all(self) -> None:
        """Close all active sessions."""
        for session in self._sessions.values():
            try:
                await session.close()
            except Exception as exc:
                logger.warning("Error closing session '%s': %s", session.server_name, exc)
        self._sessions.clear()

    def inject_into_agent_config(
        self,
        agent_config: dict[str, Any],
        server_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Inject remote MCP server configs into agent spawn config.

        For Claude Code agents, adds entries to the ``mcpServers`` structure.
        For other agents, generates tool descriptions in the system prompt.

        Args:
            agent_config: Agent configuration dict to augment.
            server_names: Subset of servers to include. Defaults to all.

        Returns:
            Updated agent configuration dict.
        """
        config = dict(agent_config)
        targets = server_names or list(self._sessions.keys())

        mcp_servers: dict[str, Any] = {}
        tool_descriptions: list[str] = []

        for name in targets:
            session = self._sessions.get(name)
            if session is None or not session.is_connected:
                continue

            # Find the config for this session
            server_cfg = session._config

            # Build mcpServers entry for Claude Code
            entry: dict[str, Any] = {"url": server_cfg.url}
            if server_cfg.auth_type == "bearer" and server_cfg.auth_token:
                entry["headers"] = {"Authorization": f"Bearer {server_cfg.auth_token}"}
            mcp_servers[name] = entry

            # Build tool descriptions for non-Claude agents
            for tool in session.tools:
                tool_descriptions.append(f"- {tool.name}: {tool.description} (server: {name})")

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

        return config
