"""Tests for the streamable HTTP transport for Bernstein MCP server."""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from unittest.mock import AsyncMock, patch

import pytest

from bernstein.core.protocols.mcp.stateless_core import LEGACY_SESSION_HEADER, REMOVAL_DATE
from bernstein.mcp import remote_transport as remote_transport_module
from bernstein.mcp.remote_transport import (
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


def _tool_result(text: str) -> dict:
    """Return a tool call's result payload, unwrapping the cost-meter envelope.

    Tool responses are wrapped in ``{"result": ..., "_meter": ...}`` by the
    per-call cost meter (issue #1674). This helper returns the inner result so
    assertions read the tool payload regardless of whether the meter is on.
    """
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        return parsed["result"]
    return parsed


def _matches_cancelled_error(node: ast.expr | None) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "CancelledError":
        return isinstance(node.value, ast.Name) and node.value.id == "asyncio"
    return isinstance(node, ast.Name) and node.id == "CancelledError"


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
# Stateless serving tests (issue #2506)
# ---------------------------------------------------------------------------


class TestStatelessServing:
    @pytest.mark.anyio
    async def test_no_state_is_retained_between_requests(self, transport: StreamableHTTPTransport) -> None:
        """The transport instance carries no per-client attribute that a
        request could populate: the same request is served identically on a
        fresh instance."""
        body = _jsonrpc_request("ping")
        first = await transport.handle_request("POST", "/mcp", {}, body)
        fresh = StreamableHTTPTransport(
            config=RemoteMCPConfig(host="127.0.0.1", path="/mcp", auth_type="none"),
            server_url="https://test:8052",
        )
        second = await fresh.handle_request("POST", "/mcp", {}, body)
        assert first == second

    @pytest.mark.anyio
    async def test_legacy_session_header_is_ignored_not_echoed(self, transport: StreamableHTTPTransport) -> None:
        status, headers, _ = await transport.handle_request(
            "POST",
            "/mcp",
            {LEGACY_SESSION_HEADER: "sess-legacy"},
            _jsonrpc_request("ping"),
        )
        assert status == 200
        assert LEGACY_SESSION_HEADER not in {k.lower() for k in headers}


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
    async def test_legacy_delete_is_a_shim_noop(self, transport: StreamableHTTPTransport) -> None:
        """There is no session to close; the legacy lifecycle is acknowledged
        as a no-op while the compat shim is active (issue #2506)."""
        status, _, _ = await transport.handle_request("DELETE", "/mcp", {LEGACY_SESSION_HEADER: "no-such"}, b"")
        assert status == 200

    @pytest.mark.anyio
    async def test_legacy_delete_refused_after_shim_window(self, config: RemoteMCPConfig) -> None:
        expired = StreamableHTTPTransport(
            config=config,
            server_url="https://test:8052",
            today=lambda: REMOVAL_DATE,
        )
        status, _, _ = await expired.handle_request("DELETE", "/mcp", {}, b"")
        assert status == 405


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
    async def test_no_session_header_returned(self, transport: StreamableHTTPTransport) -> None:
        """The transport never mints a protocol session id (issue #2506)."""
        body = _jsonrpc_request("ping")
        _, headers, _ = await transport.handle_request("POST", "/mcp", {}, body)
        assert LEGACY_SESSION_HEADER not in {k.lower() for k in headers}


# ---------------------------------------------------------------------------
# Tool execution tests
# ---------------------------------------------------------------------------


class TestToolExecution:
    def test_tools_call_does_not_catch_cancelled_error(self) -> None:
        source = inspect.getsource(remote_transport_module.StreamableHTTPTransport._method_tools_call)
        tree = ast.parse(textwrap.dedent(source))
        cancelled_handlers = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and _matches_cancelled_error(node.type)
        ]
        assert cancelled_handlers == []

    @pytest.mark.anyio
    async def test_health_tool(self, transport: StreamableHTTPTransport) -> None:
        body = _jsonrpc_request("tools/call", {"name": "bernstein_health", "arguments": {}})
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        content = data["result"]["content"]
        assert len(content) == 1
        assert _tool_result(content[0]["text"])["status"] == "ok"

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
        assert _tool_result(text)["total"] == 5

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
        text = _tool_result(data["result"]["content"][0]["text"])
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
        # The legacy header stays preflight-allowed for the compat window,
        # but no response carries it, so nothing is exposed (issue #2506).
        assert LEGACY_SESSION_HEADER in headers["access-control-allow-headers"]
        assert "access-control-expose-headers" not in headers

    def test_custom_origins(self, _clear_token_env: None) -> None:
        cfg = RemoteMCPConfig(cors_origins=["https://example.com"])
        headers = _cors_headers(cfg)
        assert headers["access-control-allow-origin"] == "https://example.com"


# ---------------------------------------------------------------------------
# Clear-text CORS origin policy
# ---------------------------------------------------------------------------


class TestClearTextCORSOrigins:
    """A clear-text browser origin is only allowed when pinned to loopback.

    Bearer tokens ride on these origins, so a plaintext origin that resolves off
    the machine would put them on the wire. The default stays clear-text because
    it is loopback-pinned by construction, not because the scheme is unchecked.
    """

    @pytest.mark.parametrize(
        "origin",
        [
            "http://localhost:*",
            "http://127.0.0.1:8053",
            "http://[::1]:*",
            "http://[::1]",
            "http://localhost",
            # Scheme and host are case-insensitive, so this is still loopback.
            "HTTP://LOCALHOST:*",
            "https://example.com",
            "https://app.example.com:443",
            # TLS is accepted regardless of how far off-box the host is.
            "https://[2001:db8::1]:8053",
            "wss://example.com",
            # Non-URL CORS tokens carry no scheme and are left alone.
            "*",
            "null",
        ],
    )
    def test_loopback_plaintext_and_tls_origins_are_accepted(
        self,
        origin: str,
        _clear_token_env: None,
    ) -> None:
        cfg = RemoteMCPConfig(cors_origins=[origin])
        assert cfg.cors_origins == [origin]

    @pytest.mark.parametrize(
        "origin",
        [
            "http://example.com",
            "http://192.168.1.10:8053",
            "http://evil.test:*",
            # A loopback-looking prefix that is really a different host.
            "http://localhost.evil.test",
            "HTTP://Example.COM",
            # Bracketed IPv6 literals are unwrapped before the loopback test,
            # so an off-box IPv6 host is refused with or without a port.
            "http://[2001:db8::1]:8053",
            "http://[2001:db8::1]",
            # ::1 is loopback, ::2 is not; the whole literal has to match.
            "http://[::2]:*",
            # Malformed authorities that hand-rolled bracket stripping used to
            # collapse to a bare "::1" and admit as loopback.
            "http://[::1]evil.test",
            "http://[::1]@evil.test",
            "http://[::1",
            # Other clear-text schemes are held to the same loopback rule.
            "ws://evil.test",
            "ftp://evil.test",
        ],
    )
    def test_non_loopback_plaintext_origin_is_refused(
        self,
        origin: str,
        _clear_token_env: None,
    ) -> None:
        with pytest.raises(RemoteMCPConfigError, match="clear-text CORS"):
            RemoteMCPConfig(cors_origins=[origin])

    def test_refusal_names_every_offending_origin(self, _clear_token_env: None) -> None:
        with pytest.raises(RemoteMCPConfigError) as excinfo:
            RemoteMCPConfig(cors_origins=["http://localhost:*", "http://a.test", "http://b.test"])
        message = str(excinfo.value)
        assert "a.test" in message
        assert "b.test" in message
        # The loopback origin is not listed as an offender.
        assert "http://localhost:*" not in message

    def test_default_origin_is_loopback_pinned(self, _clear_token_env: None) -> None:
        cfg = RemoteMCPConfig()
        assert cfg.cors_origins == ["http://localhost:*"]
        # The default survives its own policy check.
        assert not any(remote_transport_module._is_plaintext_non_loopback_origin(o) for o in cfg.cors_origins)

    def test_default_list_is_not_shared_between_configs(self, _clear_token_env: None) -> None:
        """The default must stay a fresh list, not a shared mutable global."""
        first = RemoteMCPConfig()
        second = RemoteMCPConfig()
        assert first.cors_origins is not second.cors_origins
        first.cors_origins.append("https://example.com")
        assert second.cors_origins == ["http://localhost:*"]


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
