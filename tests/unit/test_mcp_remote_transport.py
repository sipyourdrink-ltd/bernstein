"""Tests for the streamable HTTP transport for Bernstein MCP server."""

from __future__ import annotations

import ast
import inspect
import json
import logging
import textwrap
from importlib.metadata import version
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
# Audit wiring tests
# ---------------------------------------------------------------------------


class TestAuditWiring:
    """AC3: an audit chain with no journal would silently disable anchoring, so
    it is refused at construction rather than looking audited but recording
    nothing."""

    def test_audit_chain_without_journal_refused(self, config: RemoteMCPConfig, tmp_path) -> None:
        from bernstein.core.security.audit_chain import AuditChainStore

        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        with pytest.raises(ValueError, match="audit_chain requires a journal"):
            StreamableHTTPTransport(config=config, audit_chain=chain)

    def test_audit_chain_with_journal_accepted(self, config: RemoteMCPConfig, tmp_path) -> None:
        from bernstein.core.replay.journal import EventJournal
        from bernstein.core.security.audit_chain import AuditChainStore

        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        journal = EventJournal("run-audit", tmp_path / "journal")
        transport = StreamableHTTPTransport(config=config, journal=journal, audit_chain=chain)
        assert transport._audit_chain is chain

    def test_journal_without_chain_accepted(self, config: RemoteMCPConfig, tmp_path) -> None:
        from bernstein.core.replay.journal import EventJournal

        journal = EventJournal("run-audit", tmp_path / "journal")
        transport = StreamableHTTPTransport(config=config, journal=journal)
        assert transport._journal is journal


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
    async def test_batch_request_is_rejected(self, transport: StreamableHTTPTransport) -> None:
        """JSON-RPC batching left the MCP schema two revisions ago (#3084)."""
        batch = json.dumps(
            [
                {"jsonrpc": "2.0", "method": "ping", "id": 1},
                {"jsonrpc": "2.0", "method": "ping", "id": 2},
            ]
        ).encode()
        status, _, body = await transport.handle_request("POST", "/mcp", {}, batch)
        assert status == 400
        data = json.loads(body)
        assert data["error"]["code"] == -32600


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
    async def test_initialize_reports_the_package_version_across_transports(
        self, transport: StreamableHTTPTransport
    ) -> None:
        """Both MCP transports identify the installed Bernstein distribution."""
        from bernstein.mcp.server import create_mcp_server

        expected_version = version("bernstein")
        stdio_server = create_mcp_server(server_url="http://localhost:8052")
        stdio_options = stdio_server._mcp_server.create_initialization_options()

        body = _jsonrpc_request("initialize", {"clientInfo": {"name": "test-client"}})
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)

        assert status == 200
        remote_version = json.loads(resp_body)["result"]["serverInfo"]["version"]
        assert stdio_options.server_version == expected_version
        assert remote_version == expected_version
        assert remote_version != version("mcp")

    @pytest.mark.anyio
    async def test_tools_list(self, transport: StreamableHTTPTransport) -> None:
        body = _jsonrpc_request("tools/list")
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        tools = data["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert "bernstein_run" in tool_names
        assert "bernstein_status" in tool_names
        assert "bernstein_cancel" in tool_names
        # Deprecated names are callable but never advertised (#3087).
        assert "bernstein_health" not in tool_names

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
    async def test_health_tool_alias(self, transport: StreamableHTTPTransport) -> None:
        body = _jsonrpc_request("tools/call", {"name": "bernstein_health", "arguments": {}})
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        content = data["result"]["content"]
        assert len(content) == 1
        # Deprecated alias (#3087): the historical body under ``result``.
        payload = _tool_result(content[0]["text"])
        assert payload["result"]["status"] == "ok"
        assert payload["replacement"] == "bernstein_status"

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
        folded = _tool_result(text)
        assert folded["live"] is True
        assert folded["counts"]["total"] == 5

    @pytest.mark.anyio
    async def test_stop_tool_writes_signal(self, transport: StreamableHTTPTransport, tmp_path: object) -> None:
        from pathlib import Path

        workdir = Path(str(tmp_path))
        # The workdir must already be a Bernstein project root: the tool stops
        # a project that exists, it does not create a tree where it is pointed.
        (workdir / ".sdd").mkdir()
        body = _jsonrpc_request(
            "tools/call",
            {"name": "bernstein_shutdown_orchestrator", "arguments": {"workdir": str(workdir)}},
        )
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        text = _tool_result(data["result"]["content"][0]["text"])
        assert text["status"] == "shutdown signal sent"
        signal_file = workdir / ".sdd" / "runtime" / "signals" / "SHUTDOWN"
        assert signal_file.exists()

    @pytest.mark.anyio
    async def test_stop_tool_refuses_a_workdir_that_is_not_a_project(
        self, transport: StreamableHTTPTransport, tmp_path: object
    ) -> None:
        """The remote surface serves the same tool and needs the same barrier.

        Before the barrier this transport ran ``mkdir(parents=True)`` on any
        path the caller named, so a stop against an unrelated directory both
        created a ``.sdd`` tree there and dropped a SHUTDOWN file in it.
        """
        from pathlib import Path

        victim = Path(str(tmp_path)) / "victim"
        victim.mkdir()
        body = _jsonrpc_request(
            "tools/call",
            {"name": "bernstein_shutdown_orchestrator", "arguments": {"workdir": str(victim)}},
        )
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        text = _tool_result(data["result"]["content"][0]["text"])
        assert "error" in text
        assert "status" not in text
        assert list(victim.iterdir()) == []

    @pytest.mark.anyio
    async def test_stop_tool_refuses_a_workdir_the_filesystem_cannot_address(
        self, transport: StreamableHTTPTransport, tmp_path: object
    ) -> None:
        """This surface applies no tool schema, so the barrier owns the refusal.

        The stdio server bounds ``workdir`` in its tool schema before the
        handler runs. This transport serves the tool straight from the
        JSON-RPC arguments, so a workdir that cannot name a directory reaches
        the barrier unfiltered and must come back as the barrier's own
        refusal rather than a raw filesystem message.
        """
        from pathlib import Path

        unaddressable = f"{Path(str(tmp_path))}/pro\x00ject"
        body = _jsonrpc_request(
            "tools/call",
            {"name": "bernstein_shutdown_orchestrator", "arguments": {"workdir": unaddressable}},
        )
        status, _, resp_body = await transport.handle_request("POST", "/mcp", {}, body)
        assert status == 200
        data = json.loads(resp_body)
        text = _tool_result(data["result"]["content"][0]["text"])
        assert "error" in text
        assert "status" not in text
        assert "workdir" in text["error"]
        assert "lstat" not in text["error"]


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


# ---------------------------------------------------------------------------
# Approval gate over the remote transport (#3081)
# ---------------------------------------------------------------------------


class TestRemoteApprovalGate:
    """The remote transport enforces the same approval gate as the local server.

    The gate would be worthless if a caller could reach the unconditional
    completion path simply by connecting over HTTP instead of stdio.
    """

    @pytest.mark.anyio
    async def test_approve_refuses_a_task_that_is_not_awaiting_approval(
        self,
        transport: StreamableHTTPTransport,
    ) -> None:
        proxy_get = AsyncMock(return_value=json.dumps({"id": "t-1", "status": "in_progress"}))
        proxy_post = AsyncMock(return_value="{}")

        with (
            patch.object(StreamableHTTPTransport, "_proxy_get", proxy_get),
            patch.object(StreamableHTTPTransport, "_proxy_post", proxy_post),
        ):
            raw = await transport._execute_tool("bernstein_approve", {"task_id": "t-1"})

        payload = json.loads(raw)
        assert payload["error"] == "task_not_awaiting_approval"
        assert payload["current_status"] == "in_progress"
        proxy_post.assert_not_awaited()

    @pytest.mark.anyio
    async def test_approve_signs_off_a_pending_approval_task(
        self,
        transport: StreamableHTTPTransport,
    ) -> None:
        proxy_get = AsyncMock(return_value=json.dumps({"id": "t-2", "status": "pending_approval"}))
        proxy_post = AsyncMock(return_value="{}")

        with (
            patch.object(StreamableHTTPTransport, "_proxy_get", proxy_get),
            patch.object(StreamableHTTPTransport, "_proxy_post", proxy_post),
        ):
            await transport._execute_tool("bernstein_approve", {"task_id": "t-2", "note": "LGTM"})

        path, body = proxy_post.call_args[0]
        assert path == "/tasks/t-2/complete"
        assert body["result_summary"] == "LGTM"

    @pytest.mark.anyio
    async def test_approve_refuses_a_planned_task(self, transport: StreamableHTTPTransport) -> None:
        """Plan mode's decision is not granted per task, on this transport either."""
        proxy_get = AsyncMock(return_value=json.dumps({"id": "t-3", "status": "planned"}))
        proxy_post = AsyncMock(return_value="{}")

        with (
            patch.object(StreamableHTTPTransport, "_proxy_get", proxy_get),
            patch.object(StreamableHTTPTransport, "_proxy_post", proxy_post),
        ):
            raw = await transport._execute_tool("bernstein_approve", {"task_id": "t-3"})

        payload = json.loads(raw)
        assert payload["error"] == "task_not_awaiting_approval"
        assert payload["current_status"] == "planned"
        assert "plan" in payload["hint"].lower()
        proxy_post.assert_not_awaited()

    @pytest.mark.anyio
    async def test_complete_posts_the_worker_summary(self, transport: StreamableHTTPTransport) -> None:
        proxy_get = AsyncMock(return_value=json.dumps({"id": "t-4", "status": "in_progress"}))
        proxy_post = AsyncMock(return_value="{}")

        with (
            patch.object(StreamableHTTPTransport, "_proxy_get", proxy_get),
            patch.object(StreamableHTTPTransport, "_proxy_post", proxy_post),
        ):
            await transport._execute_tool(
                "bernstein_complete",
                {"task_id": "t-4", "result_summary": "shipped"},
            )

        path, body = proxy_post.call_args[0]
        assert path == "/tasks/t-4/complete"
        assert body["result_summary"] == "shipped"

    @pytest.mark.anyio
    @pytest.mark.parametrize("status", ["waiting_for_subtasks", "orphaned", "pending_approval", ""])
    async def test_complete_refuses_a_task_the_caller_is_not_executing(
        self,
        transport: StreamableHTTPTransport,
        status: str,
    ) -> None:
        """The completion gate is enforced here too, or HTTP is the way around it."""
        proxy_get = AsyncMock(return_value=json.dumps({"id": "t-5", "status": status}))
        proxy_post = AsyncMock(return_value="{}")

        with (
            patch.object(StreamableHTTPTransport, "_proxy_get", proxy_get),
            patch.object(StreamableHTTPTransport, "_proxy_post", proxy_post),
        ):
            raw = await transport._execute_tool(
                "bernstein_complete",
                {"task_id": "t-5", "result_summary": "looked done to me"},
            )

        payload = json.loads(raw)
        assert payload["error"] == "task_not_completable"
        assert payload["current_status"] == (status or "unknown")
        proxy_post.assert_not_awaited()

    @pytest.mark.anyio
    async def test_advertised_descriptions_match_the_enforced_sets(
        self,
        transport: StreamableHTTPTransport,
    ) -> None:
        """A model picks a tool from its description, so the two must not drift."""
        from bernstein.core.tasks.lifecycle import (
            APPROVABLE_TASK_STATUSES,
            WORKER_COMPLETABLE_TASK_STATUSES,
        )
        from bernstein.mcp.remote_transport import _TOOL_DEFS

        by_name = {d["name"]: d["description"] for d in _TOOL_DEFS}
        for state in APPROVABLE_TASK_STATUSES:
            assert state.value in by_name["bernstein_approve"]
        for state in WORKER_COMPLETABLE_TASK_STATUSES:
            assert state.value in by_name["bernstein_complete"]


# ---------------------------------------------------------------------------
# Interim validation-scope notice (issue #3088)
# ---------------------------------------------------------------------------


class TestValidationScopeNotice:
    """The transport must announce that it validates arguments more weakly than stdio.

    Remove this class in the same change that closes issue #3083. Until then
    it keeps the notice from being dropped while the limitation remains.
    """

    def test_starting_the_transport_warns_about_weaker_validation(
        self,
        _clear_token_env: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="bernstein.mcp.remote_transport")

        create_asgi_app()

        records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert records, "starting the streamable HTTP transport emitted no warning"
        message = "\n".join(r.getMessage() for r in records)
        assert "validation" in message.lower()
        assert "stdio" in message.lower()
        assert "#3083" in message

    def test_notice_is_at_warning_level_not_debug(
        self,
        _clear_token_env: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A debug-only notice is invisible in ordinary startup output."""
        caplog.set_level(logging.INFO, logger="bernstein.mcp.remote_transport")

        create_asgi_app()

        assert any(r.levelno >= logging.WARNING and "#3083" in r.getMessage() for r in caplog.records), (
            "the notice must be emitted at WARNING or above"
        )

    def test_notice_names_every_tool_the_transport_exposes(
        self,
        _clear_token_env: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The list is derived from _TOOL_DEFS so it cannot drift from reality."""
        caplog.set_level(logging.WARNING, logger="bernstein.mcp.remote_transport")

        create_asgi_app()

        message = "\n".join(r.getMessage() for r in caplog.records)
        exposed = [str(defn["name"]) for defn in remote_transport_module._TOOL_DEFS]
        assert str(len(exposed)) in message
        for name in exposed:
            assert name in message, f"notice does not name exposed tool {name}"


# ---------------------------------------------------------------------------
# WWW-Authenticate challenge on 401 (issue #3075)
# ---------------------------------------------------------------------------


@pytest.fixture
def _oauth_issuer(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure an OAuth issuer for the duration of a test."""
    issuer = "https://idp.example.com"
    monkeypatch.setenv("BERNSTEIN_MCP_OAUTH_ISSUER", issuer)
    monkeypatch.delenv("BERNSTEIN_MCP_OAUTH_SCOPES", raising=False)
    return issuer


@pytest.fixture
def _no_oauth_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_MCP_OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_OAUTH_SCOPES", raising=False)


_PROXY_HEADERS = {"host": "bernstein.example.com", "x-forwarded-proto": "https"}


def _www_authenticate(headers: dict[str, str]) -> str | None:
    """Return the WWW-Authenticate value regardless of header casing."""
    for key, value in headers.items():
        if key.lower() == "www-authenticate":
            return value
    return None


class TestUnauthorizedChallenge:
    """A 401 with no challenge leaves the served protected-resource metadata
    undiscoverable: the client has no path from the refusal to the document."""

    @pytest.mark.anyio
    async def test_401_carries_resource_metadata_challenge(
        self,
        bearer_transport: StreamableHTTPTransport,
        _oauth_issuer: str,
    ) -> None:
        status, headers, _ = await bearer_transport.handle_request(
            "POST",
            "/mcp",
            dict(_PROXY_HEADERS),
            _jsonrpc_request("ping"),
        )
        assert status == 401
        assert _www_authenticate(headers) == (
            'Bearer resource_metadata="https://bernstein.example.com/.well-known/oauth-protected-resource"'
        )

    @pytest.mark.anyio
    async def test_challenge_url_resolves_to_the_served_document(
        self,
        bearer_transport: StreamableHTTPTransport,
        _oauth_issuer: str,
    ) -> None:
        """The advertised URL must fetch the metadata the transport serves."""
        from urllib.parse import urlsplit

        _, headers, _ = await bearer_transport.handle_request(
            "POST",
            "/mcp",
            dict(_PROXY_HEADERS),
            _jsonrpc_request("ping"),
        )
        challenge = _www_authenticate(headers)
        assert challenge is not None
        advertised = challenge.split('resource_metadata="', 1)[1].rstrip('"')
        split = urlsplit(advertised)
        assert split.scheme == "https"
        assert split.netloc == "bernstein.example.com"

        meta_status, _, meta_body = await bearer_transport.handle_request(
            "GET",
            split.path,
            dict(_PROXY_HEADERS),
            b"",
        )
        assert meta_status == 200
        payload = json.loads(meta_body)
        assert payload["resource"] == "https://bernstein.example.com/mcp"
        assert payload["authorization_servers"] == [_oauth_issuer]

    @pytest.mark.anyio
    async def test_no_challenge_without_issuer(
        self,
        bearer_transport: StreamableHTTPTransport,
        _no_oauth_issuer: None,
    ) -> None:
        """Anonymous and static-bearer deployments are unchanged."""
        status, headers, body = await bearer_transport.handle_request(
            "POST",
            "/mcp",
            dict(_PROXY_HEADERS),
            _jsonrpc_request("ping"),
        )
        assert status == 401
        assert _www_authenticate(headers) is None
        assert body == b'{"error":"unauthorized"}'

    @pytest.mark.anyio
    async def test_401_body_is_unchanged_with_issuer(
        self,
        bearer_transport: StreamableHTTPTransport,
        _oauth_issuer: str,
    ) -> None:
        status, _, body = await bearer_transport.handle_request(
            "POST",
            "/mcp",
            dict(_PROXY_HEADERS),
            _jsonrpc_request("ping"),
        )
        assert status == 401
        assert body == b'{"error":"unauthorized"}'

    @pytest.mark.anyio
    async def test_challenge_carries_no_tenant_token_or_user_identifier(
        self,
        _oauth_issuer: str,
        _clear_token_env: None,
    ) -> None:
        """The challenge is a public pointer, so it must name nothing private."""
        cfg = RemoteMCPConfig(path="/mcp", auth_type="bearer", auth_token="super-secret-token")
        transport = StreamableHTTPTransport(config=cfg, server_url="https://test:8052")
        _, headers, _ = await transport.handle_request(
            "POST",
            "/mcp",
            {
                "host": "bernstein.example.com",
                "x-forwarded-proto": "https",
                "authorization": "Bearer presented-token-value",
                "x-bernstein-tenant": "tenant-4711",
                "x-bernstein-user": "operator-jane",
            },
            _jsonrpc_request("ping"),
        )
        challenge = _www_authenticate(headers)
        assert challenge is not None
        for secret in (
            "super-secret-token",
            "presented-token-value",
            "tenant-4711",
            "operator-jane",
        ):
            assert secret not in challenge

    @pytest.mark.anyio
    async def test_challenge_rejects_header_injection_via_host(
        self,
        bearer_transport: StreamableHTTPTransport,
        _oauth_issuer: str,
    ) -> None:
        """The Host header is client-supplied; it must not break the value."""
        _, headers, _ = await bearer_transport.handle_request(
            "POST",
            "/mcp",
            {"host": 'evil"\r\nx-injected: 1', "x-forwarded-proto": "https"},
            _jsonrpc_request("ping"),
        )
        challenge = _www_authenticate(headers)
        assert challenge is not None
        assert "\r" not in challenge
        assert "\n" not in challenge
        assert challenge.count('"') == 2

    def test_every_401_goes_through_the_challenge_helper(self) -> None:
        """Guard: a future 401 added without the helper would silently drop the
        challenge, so no other site in the module may return a bare 401."""
        source = textwrap.dedent(inspect.getsource(remote_transport_module))
        tree = ast.parse(source)
        builder = "_unauthorized_response"
        exempt: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == builder:
                exempt.update(inner.lineno for inner in ast.walk(node) if isinstance(inner, ast.Return))
        offenders: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Tuple):
                continue
            elts = node.value.elts
            if not elts:
                continue
            first = elts[0]
            if isinstance(first, ast.Constant) and first.value == 401 and node.lineno not in exempt:
                offenders.append(node.lineno)
        assert exempt, f"{builder} not found; the guard below would pass vacuously"
        assert offenders == [], (
            f"401 returned as a literal tuple at lines {offenders}; build it with "
            "_unauthorized_response so the WWW-Authenticate challenge is always attached"
        )
