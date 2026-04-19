"""Tests for MCP client — remote tool consumption."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from bernstein.core.protocols.mcp_client import (
    MCPAuthError,
    MCPClientError,
    MCPClientManager,
    MCPClientSession,
    MCPConnectionError,
    MCPToolNotFoundError,
    RemoteServerConfig,
    RemoteTool,
    ToolCallResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def basic_config() -> RemoteServerConfig:
    """Config with no auth."""
    return RemoteServerConfig(
        name="test-server",
        url="https://localhost:9090/mcp",
    )


@pytest.fixture
def bearer_config() -> RemoteServerConfig:
    """Config with bearer auth."""
    return RemoteServerConfig(
        name="secure-server",
        url="https://api.example.com/mcp",
        auth_type="bearer",
        auth_token="sk-test-token-123",
        timeout_seconds=10,
        retry_limit=1,
    )


@pytest.fixture
def oauth_config() -> RemoteServerConfig:
    """Config with OAuth auth."""
    return RemoteServerConfig(
        name="oauth-server",
        url="https://oauth.example.com/mcp",
        auth_type="oauth",
        auth_token="oauth-access-token",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
    )


_FAKE_REQUEST = httpx.Request("POST", "https://test")


def _make_jsonrpc_response(
    result: dict[str, Any],
    request_id: int = 1,
    status_code: int = 200,
    session_id: str | None = None,
) -> httpx.Response:
    """Build a mock httpx.Response with JSON-RPC payload."""

    body = {"jsonrpc": "2.0", "id": request_id, "result": result}
    headers = {"content-type": "application/json"}
    if session_id:
        headers["mcp-session-id"] = session_id
    return httpx.Response(
        status_code=status_code,
        json=body,
        headers=headers,
        request=_FAKE_REQUEST,
    )


def _make_error_response(
    code: int,
    message: str,
    request_id: int = 1,
    status_code: int = 200,
) -> httpx.Response:
    """Build a mock httpx.Response with a JSON-RPC error."""
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    return httpx.Response(
        status_code=status_code,
        json=body,
        headers={"content-type": "application/json"},
        request=_FAKE_REQUEST,
    )


# ---------------------------------------------------------------------------
# RemoteServerConfig
# ---------------------------------------------------------------------------


class TestRemoteServerConfig:
    """Tests for RemoteServerConfig dataclass."""

    def test_defaults(self) -> None:
        cfg = RemoteServerConfig(name="x", url="https://localhost")
        assert cfg.transport == "streamable-http"
        assert cfg.auth_type == "none"
        assert cfg.auth_token == ""
        assert cfg.timeout_seconds == 30
        assert cfg.retry_limit == 3

    def test_frozen(self) -> None:
        cfg = RemoteServerConfig(name="x", url="https://localhost")
        with pytest.raises(AttributeError):
            cfg.name = "y"  # type: ignore[misc]

    def test_bearer_config(self, bearer_config: RemoteServerConfig) -> None:
        assert bearer_config.auth_type == "bearer"
        assert bearer_config.auth_token == "sk-test-token-123"
        assert bearer_config.timeout_seconds == 10
        assert bearer_config.retry_limit == 1


# ---------------------------------------------------------------------------
# RemoteTool / ToolCallResult
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Tests for RemoteTool and ToolCallResult."""

    def test_remote_tool_defaults(self) -> None:
        tool = RemoteTool(name="read", description="Read a file", server_name="s1")
        assert tool.input_schema == {}

    def test_tool_call_result_defaults(self) -> None:
        result = ToolCallResult(content="ok")
        assert result.is_error is False
        assert result.metadata == {}


# ---------------------------------------------------------------------------
# MCPClientSession — auth headers
# ---------------------------------------------------------------------------


class TestAuthHeaders:
    """Tests for _build_auth_headers."""

    def test_no_auth(self, basic_config: RemoteServerConfig) -> None:
        session = MCPClientSession(basic_config)
        assert session._build_auth_headers() == {}

    def test_bearer_auth(self, bearer_config: RemoteServerConfig) -> None:
        session = MCPClientSession(bearer_config)
        headers = session._build_auth_headers()
        assert headers == {"Authorization": "Bearer sk-test-token-123"}

    def test_oauth_auth(self, oauth_config: RemoteServerConfig) -> None:
        session = MCPClientSession(oauth_config)
        headers = session._build_auth_headers()
        assert headers == {"Authorization": "Bearer oauth-access-token"}


# ---------------------------------------------------------------------------
# MCPClientSession — connect / list_tools / call_tool
# ---------------------------------------------------------------------------


class TestMCPClientSession:
    """Tests for MCPClientSession with mocked httpx."""

    @pytest.mark.asyncio
    async def test_connect_initializes(self, basic_config: RemoteServerConfig) -> None:
        """connect() sends initialize, then initialized, then tools/list."""
        call_count = 0

        async def mock_post(url: str, *, json: Any, headers: Any, **kwargs: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            method = json.get("method", "")
            if method == "initialize":
                return _make_jsonrpc_response(
                    {"serverInfo": {"name": "remote"}},
                    request_id=json["id"],
                    session_id="sess-abc",
                )
            if method == "notifications/initialized":
                return httpx.Response(200, headers={}, request=_FAKE_REQUEST)
            if method == "tools/list":
                return _make_jsonrpc_response(
                    {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo input",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                    request_id=json["id"],
                )
            return httpx.Response(200, json={}, headers={}, request=_FAKE_REQUEST)

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.protocols.mcp_client.httpx.AsyncClient", return_value=mock_client):
            session = MCPClientSession(basic_config)
            await session.connect()

        assert session.is_connected
        assert len(session.tools) == 1
        assert session.tools[0].name == "echo"
        assert session.tools[0].server_name == "test-server"
        assert session._mcp_session_id == "sess-abc"

    @pytest.mark.asyncio
    async def test_list_tools(self, basic_config: RemoteServerConfig) -> None:
        """list_tools() parses tool schemas correctly."""

        async def mock_post(url: str, *, json: Any, headers: Any, **kwargs: Any) -> httpx.Response:
            return _make_jsonrpc_response(
                {
                    "tools": [
                        {"name": "a", "description": "Tool A", "inputSchema": {"type": "object"}},
                        {"name": "b", "description": "Tool B"},
                    ]
                },
                request_id=json["id"],
            )

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.protocols.mcp_client.httpx.AsyncClient", return_value=mock_client):
            session = MCPClientSession(basic_config)
            session._initialized = True
            tools = await session.list_tools()

        assert len(tools) == 2
        assert tools[0].input_schema == {"type": "object"}
        assert tools[1].input_schema == {}

    @pytest.mark.asyncio
    async def test_call_tool_success(self, basic_config: RemoteServerConfig) -> None:
        """call_tool() returns parsed content."""

        async def mock_post(url: str, *, json: Any, headers: Any, **kwargs: Any) -> httpx.Response:
            return _make_jsonrpc_response(
                {
                    "content": [{"type": "text", "text": "hello world"}],
                    "isError": False,
                },
                request_id=json["id"],
            )

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.protocols.mcp_client.httpx.AsyncClient", return_value=mock_client):
            session = MCPClientSession(basic_config)
            session._initialized = True
            # Add a known tool so validation passes
            session._tools = [RemoteTool(name="echo", description="", server_name="test-server")]
            result = await session.call_tool("echo", {"input": "hi"})

        assert result.content == "hello world"
        assert result.is_error is False
        assert result.metadata["server"] == "test-server"

    @pytest.mark.asyncio
    async def test_call_tool_not_found(self, basic_config: RemoteServerConfig) -> None:
        """call_tool() raises MCPToolNotFoundError for unknown tools."""
        session = MCPClientSession(basic_config)
        session._tools = [RemoteTool(name="echo", description="", server_name="test-server")]

        with pytest.raises(MCPToolNotFoundError, match="not found"):
            await session.call_tool("nonexistent", {})

    @pytest.mark.asyncio
    async def test_call_tool_error_response(self, basic_config: RemoteServerConfig) -> None:
        """call_tool() propagates isError from the tool result."""

        async def mock_post(url: str, *, json: Any, headers: Any, **kwargs: Any) -> httpx.Response:
            return _make_jsonrpc_response(
                {
                    "content": [{"type": "text", "text": "something broke"}],
                    "isError": True,
                },
                request_id=json["id"],
            )

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.protocols.mcp_client.httpx.AsyncClient", return_value=mock_client):
            session = MCPClientSession(basic_config)
            session._tools = [RemoteTool(name="fail", description="", server_name="test-server")]
            result = await session.call_tool("fail", {})

        assert result.is_error is True
        assert "something broke" in result.content

    @pytest.mark.asyncio
    async def test_close(self, basic_config: RemoteServerConfig) -> None:
        """close() resets session state."""
        session = MCPClientSession(basic_config)
        session._initialized = True
        session._tools = [RemoteTool(name="x", description="", server_name="s")]

        await session.close()

        assert not session.is_connected
        assert session.tools == []


# ---------------------------------------------------------------------------
# MCPClientSession — error handling
# ---------------------------------------------------------------------------


class TestSessionErrors:
    """Tests for error conditions."""

    @pytest.mark.asyncio
    async def test_auth_failure_401(self, bearer_config: RemoteServerConfig) -> None:
        """401 response raises MCPAuthError."""
        bearer_cfg = RemoteServerConfig(
            name="s",
            url="https://x",
            auth_type="bearer",
            auth_token="bad",
            retry_limit=1,
        )

        async def mock_post(url: str, *, json: Any, headers: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(401, json={}, headers={}, request=_FAKE_REQUEST)

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.protocols.mcp_client.httpx.AsyncClient", return_value=mock_client):
            session = MCPClientSession(bearer_cfg)
            with pytest.raises(MCPAuthError, match="401"):
                await session.connect()

    @pytest.mark.asyncio
    async def test_connection_refused(self, basic_config: RemoteServerConfig) -> None:
        """ConnectError raises MCPConnectionError after retries."""
        cfg = RemoteServerConfig(name="s", url="https://x", retry_limit=2)

        async def mock_post(url: str, *, json: Any, headers: Any, **kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.protocols.mcp_client.httpx.AsyncClient", return_value=mock_client):
            session = MCPClientSession(cfg)
            with pytest.raises(MCPConnectionError, match="Cannot connect"):
                await session.connect()

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """TimeoutException raises MCPConnectionError."""
        cfg = RemoteServerConfig(name="s", url="https://x", retry_limit=1)

        async def mock_post(url: str, *, json: Any, headers: Any, **kwargs: Any) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.protocols.mcp_client.httpx.AsyncClient", return_value=mock_client):
            session = MCPClientSession(cfg)
            with pytest.raises(MCPConnectionError, match="Timeout"):
                await session.connect()

    @pytest.mark.asyncio
    async def test_jsonrpc_error(self, basic_config: RemoteServerConfig) -> None:
        """JSON-RPC error in response raises MCPClientError."""
        cfg = RemoteServerConfig(name="s", url="https://x", retry_limit=1)

        async def mock_post(url: str, *, json: Any, headers: Any, **kwargs: Any) -> httpx.Response:
            return _make_error_response(-32600, "Invalid Request", request_id=json["id"])

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.protocols.mcp_client.httpx.AsyncClient", return_value=mock_client):
            session = MCPClientSession(cfg)
            with pytest.raises(MCPClientError, match="Invalid Request"):
                await session.connect()


# ---------------------------------------------------------------------------
# MCPClientManager
# ---------------------------------------------------------------------------


class TestMCPClientManager:
    """Tests for MCPClientManager."""

    @pytest.mark.asyncio
    async def test_connect_and_get_session(self) -> None:
        """connect() stores session accessible via get_session()."""
        cfg = RemoteServerConfig(name="s1", url="https://x", retry_limit=1)
        manager = MCPClientManager()

        # Patch MCPClientSession.connect to be a no-op
        with patch.object(MCPClientSession, "connect", new_callable=AsyncMock):
            session = await manager.connect(cfg)

        assert manager.get_session("s1") is session
        assert manager.get_session("nonexistent") is None

    @pytest.mark.asyncio
    async def test_connect_all_partial_failure(self) -> None:
        """connect_all() succeeds for some servers even if others fail."""
        good = RemoteServerConfig(name="good", url="https://good", retry_limit=1)
        bad = RemoteServerConfig(name="bad", url="https://bad", retry_limit=1)

        call_count = 0

        async def mock_connect(self: MCPClientSession) -> None:
            nonlocal call_count
            call_count += 1
            if self._config.name == "bad":
                raise MCPConnectionError("fail")

        manager = MCPClientManager()
        with patch.object(MCPClientSession, "connect", mock_connect):
            sessions = await manager.connect_all([good, bad])

        assert len(sessions) == 1
        assert sessions[0].server_name == "good"

    @pytest.mark.asyncio
    async def test_discover_all_tools(self) -> None:
        """discover_all_tools() aggregates tools from all sessions."""
        manager = MCPClientManager()

        # Create two mock sessions
        s1 = MCPClientSession(RemoteServerConfig(name="s1", url="https://x"))
        s1._initialized = True
        s1._tools = [RemoteTool(name="t1", description="", server_name="s1")]

        s2 = MCPClientSession(RemoteServerConfig(name="s2", url="https://y"))
        s2._initialized = True
        s2._tools = [RemoteTool(name="t2", description="", server_name="s2")]

        manager._sessions = {"s1": s1, "s2": s2}

        # Mock list_tools to return cached tools
        async def mock_list(self: MCPClientSession) -> list[RemoteTool]:
            return list(self._tools)

        with patch.object(MCPClientSession, "list_tools", mock_list):
            tools = await manager.discover_all_tools()

        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_call_tool_delegates(self) -> None:
        """call_tool() delegates to the correct session."""
        manager = MCPClientManager()
        session = MCPClientSession(RemoteServerConfig(name="s1", url="https://x"))
        session._initialized = True
        session._tools = [RemoteTool(name="echo", description="", server_name="s1")]
        manager._sessions = {"s1": session}

        expected = ToolCallResult(content="ok")

        with patch.object(MCPClientSession, "call_tool", new_callable=AsyncMock, return_value=expected):
            result = await manager.call_tool("s1", "echo", {"msg": "hi"})

        assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_call_tool_unknown_server(self) -> None:
        """call_tool() raises for unknown server."""
        manager = MCPClientManager()
        with pytest.raises(MCPClientError, match="No active session"):
            await manager.call_tool("unknown", "tool", {})

    @pytest.mark.asyncio
    async def test_close_all(self) -> None:
        """close_all() closes and clears all sessions."""
        manager = MCPClientManager()
        s1 = MCPClientSession(RemoteServerConfig(name="s1", url="https://x"))
        s1._initialized = True
        manager._sessions = {"s1": s1}

        await manager.close_all()
        assert len(manager._sessions) == 0

    def test_inject_into_agent_config_claude_code(self) -> None:
        """inject_into_agent_config() adds mcpServers for Claude Code."""
        manager = MCPClientManager()
        cfg = RemoteServerConfig(
            name="remote",
            url="https://api.example.com/mcp",
            auth_type="bearer",
            auth_token="tok",
        )
        session = MCPClientSession(cfg)
        session._initialized = True
        session._tools = [RemoteTool(name="search", description="Search docs", server_name="remote")]
        manager._sessions = {"remote": session}

        result = manager.inject_into_agent_config({})

        assert "mcp_config" in result
        mcp_servers = result["mcp_config"]["mcpServers"]
        assert "remote" in mcp_servers
        assert mcp_servers["remote"]["url"] == "https://api.example.com/mcp"
        assert "Authorization" in mcp_servers["remote"]["headers"]

    def test_inject_into_agent_config_no_auth(self) -> None:
        """inject_into_agent_config() omits headers when auth_type is none."""
        manager = MCPClientManager()
        cfg = RemoteServerConfig(name="open", url="https://localhost:9000/mcp")
        session = MCPClientSession(cfg)
        session._initialized = True
        session._tools = []
        manager._sessions = {"open": session}

        result = manager.inject_into_agent_config({})

        assert "mcp_config" in result
        entry = result["mcp_config"]["mcpServers"]["open"]
        assert "headers" not in entry

    def test_inject_into_agent_config_merges_existing(self) -> None:
        """inject_into_agent_config() merges with existing mcp_config."""
        manager = MCPClientManager()
        cfg = RemoteServerConfig(name="new", url="https://x")
        session = MCPClientSession(cfg)
        session._initialized = True
        session._tools = []
        manager._sessions = {"new": session}

        existing_config: dict[str, Any] = {"mcp_config": {"mcpServers": {"old": {"url": "https://old"}}}}
        result = manager.inject_into_agent_config(existing_config)

        servers = result["mcp_config"]["mcpServers"]
        assert "old" in servers
        assert "new" in servers

    def test_inject_filters_by_server_names(self) -> None:
        """inject_into_agent_config() filters to specified servers."""
        manager = MCPClientManager()
        for name in ("a", "b"):
            cfg = RemoteServerConfig(name=name, url=f"https://{name}")
            s = MCPClientSession(cfg)
            s._initialized = True
            s._tools = []
            manager._sessions[name] = s

        result = manager.inject_into_agent_config({}, server_names=["a"])
        servers = result["mcp_config"]["mcpServers"]
        assert "a" in servers
        assert "b" not in servers

    def test_inject_skips_disconnected(self) -> None:
        """inject_into_agent_config() skips sessions that are not connected."""
        manager = MCPClientManager()
        cfg = RemoteServerConfig(name="dead", url="https://x")
        session = MCPClientSession(cfg)
        session._initialized = False  # not connected
        manager._sessions = {"dead": session}

        result = manager.inject_into_agent_config({})
        assert "mcp_config" not in result
