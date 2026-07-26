"""Remote transport hardening: resources, protocol version, origin (#3084).

Four defects on one handler, pinned here:

  * ``resources/list`` / ``resources/templates/list`` / ``resources/read``
    were absent, so the capability card and the lineage records were
    unreachable over the only multi-user deployment mode.
  * ``MCP-Protocol-Version`` was never read, so an unsupported revision was
    served instead of refused with a chance to downgrade.
  * The ``Origin`` request header was never read; ``cors_origins`` only
    built a response header, which is not a defence against DNS rebinding.
  * A JSON array body was fanned out as a batch, a shape removed from the
    spec two revisions ago.

Both refusals (origin, version) ship behind ``BERNSTEIN_MCP_REMOTE_HEADER_CHECKS``
as a one-release opt-out, since they refuse requests that succeed today.
"""

from __future__ import annotations

import json

import pytest

from bernstein.mcp.remote_transport import (
    RemoteMCPConfig,
    StreamableHTTPTransport,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_MCP_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_REMOTE_HEADER_CHECKS", raising=False)
    monkeypatch.delenv("BERNSTEIN_LINEAGE_MCP_ENABLED", raising=False)


@pytest.fixture
def transport(_clean_env: None) -> StreamableHTTPTransport:
    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    return StreamableHTTPTransport(config=cfg, server_url="http://test:8052")


def _req(method: str, params: dict | None = None, req_id: int = 1) -> bytes:
    msg: dict = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg).encode()


# ---------------------------------------------------------------------------
# Origin enforcement
# ---------------------------------------------------------------------------


async def test_disallowed_origin_returns_403(transport: StreamableHTTPTransport) -> None:
    status, _, body = await transport.handle_request("POST", "/mcp", {"origin": "http://evil.example"}, _req("ping"))
    assert status == 403
    assert b"origin" in body.lower()


async def test_allowed_origin_is_served(transport: StreamableHTTPTransport) -> None:
    # The default allow list pins clear-text origins to loopback with a port
    # glob, so any localhost port is an allowed browser origin.
    status, _, _body = await transport.handle_request("POST", "/mcp", {"origin": "http://localhost:5173"}, _req("ping"))
    assert status == 200


async def test_absent_origin_is_served(transport: StreamableHTTPTransport) -> None:
    # Non-browser clients send no Origin; absence is not a mismatch.
    status, _, _body = await transport.handle_request("POST", "/mcp", {}, _req("ping"))
    assert status == 200


async def test_origin_check_has_a_one_release_opt_out(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_REMOTE_HEADER_CHECKS", "0")
    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg, server_url="http://test:8052")
    status, _, _body = await transport.handle_request("POST", "/mcp", {"origin": "http://evil.example"}, _req("ping"))
    assert status == 200


# ---------------------------------------------------------------------------
# Protocol-version enforcement
# ---------------------------------------------------------------------------


async def test_unsupported_protocol_version_returns_400(transport: StreamableHTTPTransport) -> None:
    status, _, body = await transport.handle_request(
        "POST", "/mcp", {"mcp-protocol-version": "1999-01-01"}, _req("ping")
    )
    assert status == 400
    # The refusal names the supported revisions so the client can downgrade.
    assert b"2025-03-26" in body


async def test_supported_protocol_version_is_served(transport: StreamableHTTPTransport) -> None:
    status, _, _body = await transport.handle_request(
        "POST", "/mcp", {"mcp-protocol-version": "2025-03-26"}, _req("ping")
    )
    assert status == 200


async def test_absent_protocol_version_is_served_as_default(transport: StreamableHTTPTransport) -> None:
    status, _, _body = await transport.handle_request("POST", "/mcp", {}, _req("ping"))
    assert status == 200


async def test_version_check_shares_the_opt_out(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_REMOTE_HEADER_CHECKS", "off")
    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg, server_url="http://test:8052")
    status, _, _body = await transport.handle_request(
        "POST", "/mcp", {"mcp-protocol-version": "1999-01-01"}, _req("ping")
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Batch (JSON array) rejection
# ---------------------------------------------------------------------------


async def test_json_array_body_is_rejected(transport: StreamableHTTPTransport) -> None:
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
# Negotiated revision in initialize and the capability card
# ---------------------------------------------------------------------------


async def test_initialize_echoes_a_supported_requested_version(
    transport: StreamableHTTPTransport,
) -> None:
    body = _req("initialize", {"clientInfo": {"name": "t"}, "protocolVersion": "2025-06-18"})
    status, _, resp = await transport.handle_request("POST", "/mcp", {}, body)
    assert status == 200
    result = json.loads(resp)["result"]
    assert result["protocolVersion"] == "2025-06-18"
    # The card reports the negotiated revision, not a constant.
    assert result["capabilityCard"]["specRevision"] == "2025-06-18"


async def test_initialize_falls_back_on_an_unknown_requested_version(
    transport: StreamableHTTPTransport,
) -> None:
    body = _req("initialize", {"clientInfo": {"name": "t"}, "protocolVersion": "1999-01-01"})
    status, _, resp = await transport.handle_request("POST", "/mcp", {}, body)
    assert status == 200
    result = json.loads(resp)["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilityCard"]["specRevision"] == "2025-03-26"


# ---------------------------------------------------------------------------
# Resources over the remote transport
# ---------------------------------------------------------------------------


async def test_resources_list_serves_capability_and_skill_index(
    transport: StreamableHTTPTransport,
) -> None:
    status, _, resp = await transport.handle_request("POST", "/mcp", {}, _req("resources/list"))
    assert status == 200
    resources = json.loads(resp)["result"]["resources"]
    uris = {r["uri"] for r in resources}
    assert "bernstein://capability" in uris
    assert "bernstein://skills/index" in uris
    # Lineage stays dark remotely unless explicitly enabled (ADR-009 7.3).
    assert not any(u.startswith("lineage://") for u in uris)


async def test_resources_read_serves_the_capability_card(
    transport: StreamableHTTPTransport,
) -> None:
    status, _, resp = await transport.handle_request(
        "POST",
        "/mcp",
        {"mcp-protocol-version": "2025-06-18"},
        _req("resources/read", {"uri": "bernstein://capability"}),
    )
    assert status == 200
    contents = json.loads(resp)["result"]["contents"]
    card = json.loads(contents[0]["text"])
    assert card["name"] == "bernstein"
    # Read through the live request's negotiated revision, not a constant.
    assert card["specRevision"] == "2025-06-18"


async def test_resources_read_requires_auth_like_tool_calls(_clean_env: None) -> None:
    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="bearer", auth_token="sekrit")
    transport = StreamableHTTPTransport(config=cfg, server_url="http://test:8052")
    status, _, _resp = await transport.handle_request(
        "POST", "/mcp", {}, _req("resources/read", {"uri": "bernstein://capability"})
    )
    assert status == 401


async def test_lineage_resource_is_refused_when_not_enabled(
    transport: StreamableHTTPTransport,
) -> None:
    """The below-tier caller cannot read lineage remotely (default posture)."""
    status, _, resp = await transport.handle_request(
        "POST", "/mcp", {}, _req("resources/read", {"uri": "lineage://stats"})
    )
    assert status == 200
    data = json.loads(resp)
    assert "error" in data
    assert data["error"]["code"] == -32002


async def test_lineage_resources_served_when_enabled(
    _clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    monkeypatch.setenv("BERNSTEIN_LINEAGE_MCP_ENABLED", "1")
    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(
        config=cfg,
        server_url="http://test:8052",
        lineage_root=Path(str(tmp_path)) / "lineage",
    )
    status, _, resp = await transport.handle_request("POST", "/mcp", {}, _req("resources/list"))
    uris = {r["uri"] for r in json.loads(resp)["result"]["resources"]}
    assert "lineage://stats" in uris

    status, _, resp = await transport.handle_request(
        "POST", "/mcp", {}, _req("resources/read", {"uri": "lineage://stats"})
    )
    assert status == 200
    stats = json.loads(json.loads(resp)["result"]["contents"][0]["text"])
    assert stats["total_entries"] == 0

    status, _, resp = await transport.handle_request("POST", "/mcp", {}, _req("resources/templates/list"))
    templates = {t["uriTemplate"] for t in json.loads(resp)["result"]["resourceTemplates"]}
    assert "lineage://artefact/{artefact_path}" in templates


async def test_resources_read_unknown_uri_is_an_error(transport: StreamableHTTPTransport) -> None:
    status, _, resp = await transport.handle_request(
        "POST", "/mcp", {}, _req("resources/read", {"uri": "bernstein://nope"})
    )
    assert status == 200
    data = json.loads(resp)
    assert data["error"]["code"] == -32002


# ---------------------------------------------------------------------------
# Deprecated tool names on the remote surface (#3087 parity)
# ---------------------------------------------------------------------------


async def test_remote_tools_list_advertises_the_consolidated_names(
    transport: StreamableHTTPTransport,
) -> None:
    _status, _, resp = await transport.handle_request("POST", "/mcp", {}, _req("tools/list"))
    names = {t["name"] for t in json.loads(resp)["result"]["tools"]}
    assert {
        "bernstein_run",
        "bernstein_status",
        "bernstein_approve",
        "bernstein_complete",
        "bernstein_cancel",
        "bernstein_shutdown_orchestrator",
    } <= names
    assert not {"bernstein_health", "bernstein_tasks", "bernstein_cost", "bernstein_stop"} & names


async def test_remote_health_alias_names_its_replacement(
    transport: StreamableHTTPTransport,
) -> None:
    status, _, resp = await transport.handle_request(
        "POST", "/mcp", {}, _req("tools/call", {"name": "bernstein_health", "arguments": {}})
    )
    assert status == 200
    text = json.loads(resp)["result"]["content"][0]["text"]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    assert parsed["deprecated"] is True
    assert parsed["replacement"] == "bernstein_status"
    assert parsed["result"] == {"status": "ok"}
