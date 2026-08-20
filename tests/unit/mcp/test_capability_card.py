"""Tests for runtime capability cards (issue #1674).

Covers:

  * the card shape (transports, auth, tiers, meter, spec rev);
  * the card reflecting live process state (active tier, meter toggle, token);
  * the FastMCP resource exposing the card;
  * the streamable HTTP transport emitting the card on ``initialize``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bernstein.mcp.capability import (
    CAPABILITY_RESOURCE_URI,
    SPEC_REVISION,
    build_capability_card,
)


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_MCP_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_TOOL_TIER", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_COST_METER", raising=False)


# ---------------------------------------------------------------------------
# Card shape
# ---------------------------------------------------------------------------


def test_card_has_core_dimensions(_clean_env: None) -> None:
    card = build_capability_card()
    assert card["name"] == "bernstein"
    assert card["specRevision"] == SPEC_REVISION
    for key in ("transports", "auth", "tools", "costMeter", "observability", "version", "repo_url"):
        assert key in card


def test_card_version_matches_package_metadata(_clean_env: None) -> None:
    from importlib.metadata import version

    card = build_capability_card()
    assert card["version"] == version("bernstein")


def test_card_version_matches_remote_transport_server_info(_clean_env: None) -> None:
    from bernstein.mcp.remote_transport import _SERVER_INFO

    card = build_capability_card()
    assert card["version"] == _SERVER_INFO["version"]


def test_card_repo_url_matches_package_metadata(_clean_env: None) -> None:
    from importlib.metadata import metadata

    meta = metadata("bernstein")
    expected_urls = [
        url.partition(",")[2].strip()
        for url in meta.get_all("Project-URL") or []
        if url.partition(",")[0].strip().lower() in ("repository", "source")
    ]
    card = build_capability_card()
    assert card["repo_url"] in expected_urls
    assert card["repo_url"] != ""


def test_card_lists_all_transports(_clean_env: None) -> None:
    card = build_capability_card()
    types = {t["type"] for t in card["transports"]}
    assert {"stdio", "sse", "http"} <= types
    http = next(t for t in card["transports"] if t["type"] == "http")
    assert http["cancellable"] is True
    assert http["streaming"] is True


def test_card_reports_auth_modes(_clean_env: None) -> None:
    card = build_capability_card()
    assert "anonymous" in card["auth"]["supported"]
    assert "bearer" in card["auth"]["supported"]
    assert card["auth"]["bearer_token_configured"] is False
    # OAuth/OIDC are acknowledged-but-not-implemented, not silently missing.
    assert "oauth2_pkce" in card["auth"]["planned"]


def test_card_token_flag_tracks_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_TOKEN", "tok")
    card = build_capability_card()
    assert card["auth"]["bearer_token_configured"] is True


# ---------------------------------------------------------------------------
# Live state reflection
# ---------------------------------------------------------------------------


def test_card_reflects_active_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_TOOL_TIER", "core")
    card = build_capability_card()
    assert card["tools"]["activeTier"] == "core"
    assert "bernstein_cost" not in card["tools"]["advertised"]


def test_card_reflects_meter_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "0")
    card = build_capability_card()
    assert card["costMeter"]["enabled"] is False
    assert card["observability"]["perCallMeter"] is False


# ---------------------------------------------------------------------------
# FastMCP resource
# ---------------------------------------------------------------------------


def test_server_exposes_capability_resource(_clean_env: None) -> None:
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(tier="standard")
    resources = asyncio.run(mcp.list_resources())
    uris = {str(r.uri) for r in resources}
    assert CAPABILITY_RESOURCE_URI in uris


def test_capability_resource_returns_card(_clean_env: None) -> None:
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(tier="standard")
    contents = asyncio.run(mcp.read_resource(CAPABILITY_RESOURCE_URI))
    body = next(iter(contents)).content
    card = json.loads(body)
    assert card["name"] == "bernstein"
    assert card["tools"]["activeTier"] == "standard"


# ---------------------------------------------------------------------------
# Streamable HTTP transport initialize
# ---------------------------------------------------------------------------


def test_http_initialize_carries_capability_card(_clean_env: None) -> None:
    from bernstein.mcp.remote_transport import (
        RemoteMCPConfig,
        StreamableHTTPTransport,
    )

    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {"clientInfo": {"name": "test"}},
        }
    ).encode()
    status, _, resp_body = asyncio.run(transport.handle_request("POST", "/mcp", {}, body))
    assert status == 200
    result = json.loads(resp_body)["result"]
    assert "capabilityCard" in result
    assert result["capabilityCard"]["specRevision"] == SPEC_REVISION
    # The static spec capabilities object is still present.
    assert "capabilities" in result
