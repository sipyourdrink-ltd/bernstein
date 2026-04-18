"""Tests for the streamable HTTP transport for Bernstein MCP server."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from bernstein.mcp.remote_transport import (
    MCPSession,
    RemoteMCPConfig,
    RemoteMCPConfigError,
    StreamableHTTPTransport,
    _cors_headers,
    create_asgi_app,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _clear_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure env-provided tokens don't leak between tests."""
    monkeypatch.delenv("BERNSTEIN_MCP_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_AUTH_TOKEN", raising=False)


@pytest.fixture
def config(_clear_token_env: None) -> RemoteMCPConfig:
    # Loopback + bearer token is the new safe default. Keeping auth_type='none'
    # only works for loopback binds, which is still useful for tests that focus
    # on routing/session behaviour without auth overhead.
    return RemoteMCPConfig(host="127.0.0.1", path="/mcp", auth_type="none")


@pytest.fixture
def transport(config: RemoteMCPConfig) -> StreamableHTTPTransport:
    return StreamableHTTPTransport(config=config, server_url="https://test:8052")


@pytest.fixture
def bearer_config(_clear_token_env: None) -> RemoteMCPConfig:
    return RemoteMCPConfig(path="/mcp", auth_type="bearer", auth_token="secret-token")


@pytest.fixture
def bearer_transport(bearer_config: RemoteMCPConfig) -> StreamableHTTPTransport:
    return StreamableHTTPTransport(config=bearer_config, server_url="https://test:8052")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jsonrpc_request(method: str, params: dict | None = None, req_id: int = 1) -> bytes:
    msg: dict = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg).encode()


def _jsonrpc_notification(method: str, params: dict | None = None) -> bytes:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg).encode()


# ---------------------------------------------------------------------------
# RemoteMCPConfig tests
# ---------------------------------------------------------------------------


class TestRemoteMCPConfig:
    def test_defaults_bind_localhost_with_bearer_auth(self, _clear_token_env: None) -> None:
        """Default config must be safe: loopback + bearer auth required.

        This is the contract for audit-116: no ambient network exposure and
        no anonymous dispatch even if someone forgets to override the defaults.
        """
        cfg = RemoteMCPConfig()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8053
        assert cfg.path == "/mcp"
        assert cfg.auth_type == "bearer"
        assert cfg.auth_token == ""
        assert cfg.cors_origins == ["http://localhost:*"]
        assert cfg.max_sessions == 100
        assert cfg.session_timeout_seconds == 3600

    def test_frozen(self, _clear_token_env: None) -> None:
        cfg = RemoteMCPConfig()
        with pytest.raises(AttributeError):
            cfg.port = 9999  # type: ignore[misc]

    def test_explicit_public_bind_without_token_refuses(self, _clear_token_env: None) -> None:
        """Binding to 0.0.0.0 with no token must be refused at config time."""
        with pytest.raises(RemoteMCPConfigError, match="not loopback"):
            RemoteMCPConfig(host="0.0.0.0", auth_type="bearer", auth_token="")

    def test_public_bind_with_auth_none_refuses(self, _clear_token_env: None) -> None:
        """auth_type='none' on a public interface must be refused."""
        with pytest.raises(RemoteMCPConfigError, match="auth_type='none'"):
            RemoteMCPConfig(host="0.0.0.0", auth_type="none")

    def test_public_bind_with_explicit_token_allowed(self, _clear_token_env: None) -> None:
        cfg = RemoteMCPConfig(host="0.0.0.0", auth_type="bearer", auth_token="abc")
        assert cfg.auth_token == "abc"

    def test_public_bind_picks_up_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_MCP_TOKEN", "from-env")
        monkeypatch.delenv("BERNSTEIN_MCP_AUTH_TOKEN", raising=False)
        cfg = RemoteMCPConfig(host="0.0.0.0")
        assert cfg.auth_token == "from-env"

    def test_localhost_with_auth_none_allowed(self, _clear_token_env: None) -> None:
        # Binding to loopback with auth disabled is still allowed: any caller
        # is already on-box and the attack surface is equivalent to stdio.
        cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
        assert cfg.host == "127.0.0.1"


# ---------------------------------------------------------------------------
# MCPSession tests
# ---------------------------------------------------------------------------


class TestMCPSession:
    def test_creation(self) -> None:
        session = MCPSession(session_id="test-123")
        assert session.session_id == "test-123"
        assert session.tools_listed is False
        assert isinstance(session.created_at, float)

    def test_mutable(self) -> None:
        session = MCPSession(session_id="test-123")
        session.tools_listed = True
        assert session.tools_listed is True


# ---------------------------------------------------------------------------
# Authentication tests
# ---------------------------------------------------------------------------


class TestAuthentication:
    @pytest.mark.anyio
    async def test_no_auth_always_passes(self, transport: StreamableHTTPTransport) -> None:
        assert transport._authenticate({}) is True
        assert transport._authenticate({"authorization": "whatever"}) is True

    @pytest.mark.anyio
    async def test_bearer_auth_valid(self, bearer_transport: StreamableHTTPTransport) -> None:
        assert bearer_transport._authenticate({"authorization": "Bearer secret-token"}) is True

    @pytest.mark.anyio
    async def test_bearer_auth_missing(self, bearer_transport: StreamableHTTPTransport) -> None:
        assert bearer_transport._authenticate({}) is False

    @pytest.mark.anyio
    async def test_bearer_auth_wrong_token(self, bearer_transport: StreamableHTTPTransport) -> None:
        assert bearer_transport._authenticate({"authorization": "Bearer wrong"}) is False

    @pytest.mark.anyio
    async def test_bearer_auth_wrong_scheme(self, bearer_transport: StreamableHTTPTransport) -> None:
        assert bearer_transport._authenticate({"authorization": "Basic secret-token"}) is False

    @pytest.mark.anyio
    async def test_bearer_auth_empty_expected_rejects_all(self, _clear_token_env: None) -> None:
        """Defence in depth: even if someone forces bearer+empty-token on
        localhost, no request may pass auth with a blank token."""
        cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="bearer", auth_token="")
        t = StreamableHTTPTransport(config=cfg)
        assert t._authenticate({"authorization": "Bearer "}) is False
        assert t._authenticate({"authorization": "Bearer anything"}) is False


# ---------------------------------------------------------------------------
# End-to-end auth at HTTP layer
# ---------------------------------------------------------------------------


class TestHTTPAuthEnforcement:
    @pytest.mark.anyio
    async def test_missing_token_returns_401(self, bearer_transport: StreamableHTTPTransport) -> None:
        status, _, body = await bearer_transport.handle_request("POST", "/mcp", {}, _jsonrpc_request("ping"))
        assert status == 401
        assert b"unauthorized" in body

    @pytest.mark.anyio
    async def test_wrong_token_returns_401(self, bearer_transport: StreamableHTTPTransport) -> None:
        status, _, _ = await bearer_transport.handle_request(
            "POST",
            "/mcp",
            {"authorization": "Bearer not-the-right-one"},
            _jsonrpc_request("ping"),
        )
        assert status == 401

    @pytest.mark.anyio
    async def test_valid_token_accepted(self, bearer_transport: StreamableHTTPTransport) -> None:
        status, _, body = await bearer_transport.handle_request(
            "POST",
            "/mcp",
            {"authorization": "Bearer secret-token"},
            _jsonrpc_request("ping"),
        )
        assert status == 200
        assert json.loads(body)["result"] == {}


# ---------------------------------------------------------------------------
# Session management tests
# ---------------------------------------------------------------------------


class TestSessionManagement:
    @pytest.mark.anyio
    async def test_creates_new_session(self, transport: StreamableHTTPTransport) -> None:
        session = await transport._get_or_create_session(None)
        assert session.session_id in transport._sessions

    @pytest.mark.anyio
    async def test_reuses_existing_session(self, transport: StreamableHTTPTransport) -> None:
        s1 = await transport._get_or_create_session(None)
        s2 = await transport._get_or_create_session(s1.session_id)
        assert s1.session_id == s2.session_id

    @pytest.mark.anyio
    async def test_creates_new_if_id_unknown(self, transport: StreamableHTTPTransport) -> None:
        s = await transport._get_or_create_session("nonexistent-id")
        assert s.session_id != "nonexistent-id"

    @pytest.mark.anyio
    async def test_max_sessions_enforced(self, _clear_token_env: None) -> None:
        cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none", max_sessions=2)
        t = StreamableHTTPTransport(config=cfg)
        await t._get_or_create_session(None)
        await t._get_or_create_session(None)
        with pytest.raises(ValueError, match="Max sessions"):
            await t._get_or_create_session(None)

    @pytest.mark.anyio
    async def test_expired_sessions_pruned(self, _clear_token_env: None) -> None:
        cfg = RemoteMCPConfig(
            host="127.0.0.1",
            auth_type="none",
            max_sessions=1,
            session_timeout_seconds=0,
        )
        t = StreamableHTTPTransport(config=cfg)
        s1 = await t._get_or_create_session(None)
        # Force expiry by backdating.
        s1.last_active = time.time() - 10
        # Should prune s1 and create a new one.
        s2 = await t._get_or_create_session(None)
        assert s2.session_id != s1.session_id
        assert len(t._sessions) == 1


# ---------------------------------------------------------------------------
# HTTP routing tests
# ---------------------------------------------------------------------------


class TestHTTPRouting:
    @pytest.mark.anyio
    async def test_wrong_path_returns_404(self, transport: StreamableHTTPTransport) -> None:
        status, _, _ = await transport.handle_request("POST", "/wrong", {}, b"")
        assert status == 404

    @pytest.mark.anyio
    async def test_unsupported_method_returns_405(self, transport: StreamableHTTPTransport) -> None:
        status, headers, _ = await transport.handle_request("PUT", "/mcp", {}, b"")
        assert status == 405
        assert "allow" in headers

    @pytest.mark.anyio
    async def test_auth_failure_returns_401(self, bearer_transport: StreamableHTTPTransport) -> None:
        status, _, _ = await bearer_transport.handle_request("POST", "/mcp", {}, b"{}")
        assert status == 401

    @pytest.mark.anyio
    async def test_get_returns_501(self, transport: StreamableHTTPTransport) -> None:
        status, _, _ = await transport.handle_request("GET", "/mcp", {}, b"")
        assert status == 501

    @pytest.mark.anyio
    async def test_delete_unknown_session_returns_404(self, transport: StreamableHTTPTransport) -> None:
        status, _, _ = await transport.handle_request("DELETE", "/mcp", {"mcp-session-id": "no-such"}, b"")
        assert status == 404

    @pytest.mark.anyio
    async def test_delete_existing_session(self, transport: StreamableHTTPTransport) -> None:
        # Create a session first.
        status, headers, _body = await transport.handle_request(
            "POST",
            "/mcp",
            {},
            _jsonrpc_request("initialize", {"clientInfo": {"name": "test"}}),
        )
        assert status == 200
        sid = headers["mcp-session-id"]

        # Delete it.
        status, _, _ = await transport.handle_request("DELETE", "/mcp", {"mcp-session-id": sid}, b"")
        assert status == 200
        assert sid not in transport._sessions


# ---------------------------------------------------------------------------
# JSON-RPC dispatch tests
# ---------------------------------------------------------------------------


class TestJSONRPCDispatch:
    @pytest.mark.anyio
    async def test_parse_error(self, transport: StreamableHTTPTransport) -> None:
        status, _, body = await transport.handle_request("POST", "/mcp", {}, b"not json")
        assert status == 400
        data = json.loads(body)
        assert data["error"]["code"] == -32700

    @pytest.mark.anyio
    async def test_invalid_jsonrpc_version(self, transport: StreamableHTTPTransport) -> None:
        msg = json.dumps({"jsonrpc": "1.0", "method": "ping", "id": 1}).encode()
        status, _, body = await transport.handle_request("POST", "/mcp", {}, msg)
        assert status == 200
        data = json.loads(body)
        assert data["error"]["code"] == -32600

    @pytest.mark.anyio
    async def test_method_not_found(self, transport: StreamableHTTPTransport) -> None:
        status, _, body = await transport.handle_request("POST", "/mcp", {}, _jsonrpc_request("nonexistent"))
        assert status == 200
        data = json.loads(body)
        assert data["error"]["code"] == -32601

    @pytest.mark.anyio
    async def test_notification_returns_204(self, transport: StreamableHTTPTransport) -> None:
        status, _, _body = await transport.handle_request(
            "POST", "/mcp", {}, _jsonrpc_notification("notifications/initialized")
        )
        assert status == 204

    @pytest.mark.anyio
    async def test_batch_request(self, transport: StreamableHTTPTransport) -> None:
        batch = json.dumps(
            [
                {"jsonrpc": "2.0", "method": "ping", "id": 1},
                {"jsonrpc": "2.0", "method": "ping", "id": 2},
            ]
        ).encode()
        status, _, body = await transport.handle_request("POST", "/mcp", {}, batch)
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, list)
        assert len(data) == 2


# ---------------------------------------------------------------------------
# MCP method tests
# ---------------------------------------------------------------------------


class TestMCPMethods:
    @pytest.mark.anyio
    async def test_initialize(self, transport: StreamableHTTPTransport) -> None:
        body = _jsonrpc_request("initialize", {"clientInfo": {"name": "test-client"}})
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        result = data["result"]
        assert result["serverInfo"]["name"] == "bernstein"
        assert "capabilities" in result

    @pytest.mark.anyio
    async def test_tools_list(self, transport: StreamableHTTPTransport) -> None:
        body = _jsonrpc_request("tools/list")
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        tools = data["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert "bernstein_health" in tool_names
        assert "bernstein_run" in tool_names
        assert "bernstein_status" in tool_names

    @pytest.mark.anyio
    async def test_ping(self, transport: StreamableHTTPTransport) -> None:
        body = _jsonrpc_request("ping")
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        assert data["result"] == {}

    @pytest.mark.anyio
    async def test_session_id_returned(self, transport: StreamableHTTPTransport) -> None:
        body = _jsonrpc_request("ping")
        _, headers, _ = await transport.handle_request("POST", "/mcp", {}, body)
        assert "mcp-session-id" in headers


# ---------------------------------------------------------------------------
# Tool execution tests
# ---------------------------------------------------------------------------


class TestToolExecution:
    @pytest.mark.anyio
    async def test_health_tool(self, transport: StreamableHTTPTransport) -> None:
        body = _jsonrpc_request("tools/call", {"name": "bernstein_health", "arguments": {}})
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        content = data["result"]["content"]
        assert len(content) == 1
        assert json.loads(content[0]["text"])["status"] == "ok"

    @pytest.mark.anyio
    async def test_unknown_tool_returns_error(self, transport: StreamableHTTPTransport) -> None:
        body = _jsonrpc_request("tools/call", {"name": "no_such_tool", "arguments": {}})
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        assert data["result"]["isError"] is True

    @pytest.mark.anyio
    async def test_status_tool_proxies(self, transport: StreamableHTTPTransport) -> None:
        mock_response = AsyncMock()
        mock_response.text = '{"total": 5}'
        mock_response.raise_for_status = lambda: None

        with patch("bernstein.mcp.remote_transport.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = instance

            body = _jsonrpc_request("tools/call", {"name": "bernstein_status", "arguments": {}})
            status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)

        assert status == 200
        data = json.loads(resp_body)
        text = data["result"]["content"][0]["text"]
        assert json.loads(text)["total"] == 5

    @pytest.mark.anyio
    async def test_stop_tool_writes_signal(self, transport: StreamableHTTPTransport, tmp_path: object) -> None:
        from pathlib import Path

        workdir = Path(str(tmp_path))
        body = _jsonrpc_request(
            "tools/call",
            {"name": "bernstein_stop", "arguments": {"workdir": str(workdir)}},
        )
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        text = json.loads(data["result"]["content"][0]["text"])
        assert text["status"] == "shutdown signal sent"
        signal_file = workdir / ".sdd" / "runtime" / "signals" / "SHUTDOWN"
        assert signal_file.exists()


# ---------------------------------------------------------------------------
# CORS headers tests
# ---------------------------------------------------------------------------


class TestCORSHeaders:
    def test_default_cors_localhost_only(self, _clear_token_env: None) -> None:
        cfg = RemoteMCPConfig()
        headers = _cors_headers(cfg)
        assert headers["access-control-allow-origin"] == "http://localhost:*"
        assert "mcp-session-id" in headers["access-control-expose-headers"]

    def test_custom_origins(self, _clear_token_env: None) -> None:
        cfg = RemoteMCPConfig(cors_origins=["https://example.com"])
        headers = _cors_headers(cfg)
        assert headers["access-control-allow-origin"] == "https://example.com"


# ---------------------------------------------------------------------------
# ASGI app tests
# ---------------------------------------------------------------------------


class TestASGIApp:
    def test_create_asgi_app_returns_callable(self, _clear_token_env: None) -> None:
        app = create_asgi_app()
        assert callable(app)

    def test_create_asgi_app_with_config(self, _clear_token_env: None) -> None:
        cfg = RemoteMCPConfig(host="127.0.0.1", port=9999, auth_type="none")
        app = create_asgi_app(config=cfg)
        assert callable(app)


# ---------------------------------------------------------------------------
# Proxy auth propagation (audit-120)
# ---------------------------------------------------------------------------


class TestProxyAuthHeader:
    @pytest.mark.anyio
    async def test_proxy_get_forwards_bearer_token(
        self,
        transport: StreamableHTTPTransport,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_proxy_get forwards BERNSTEIN_AUTH_TOKEN as an Authorization header."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "remote-tok")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = "{}"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("bernstein.mcp.remote_transport.httpx.AsyncClient", return_value=mock_client):
            await transport._proxy_get("/status")

        headers = mock_client.get.call_args.kwargs.get("headers") or {}
        assert headers.get("Authorization") == "Bearer remote-tok"

    @pytest.mark.anyio
    async def test_proxy_get_omits_header_when_token_unset(
        self,
        transport: StreamableHTTPTransport,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_proxy_get sends no Authorization header when the env var is unset."""
        from unittest.mock import MagicMock

        monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = "{}"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("bernstein.mcp.remote_transport.httpx.AsyncClient", return_value=mock_client):
            await transport._proxy_get("/status")

        headers = mock_client.get.call_args.kwargs.get("headers") or {}
        assert "Authorization" not in headers

    @pytest.mark.anyio
    async def test_proxy_post_forwards_bearer_token(
        self,
        transport: StreamableHTTPTransport,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_proxy_post forwards BERNSTEIN_AUTH_TOKEN as an Authorization header."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "post-tok")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = "{}"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("bernstein.mcp.remote_transport.httpx.AsyncClient", return_value=mock_client):
            await transport._proxy_post("/tasks", {"title": "x"})

        headers = mock_client.post.call_args.kwargs.get("headers") or {}
        assert headers.get("Authorization") == "Bearer post-tok"
