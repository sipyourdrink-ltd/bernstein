"""Tests for OAuth-2 / OIDC discovery metadata (issue #1674, #1722).

Bernstein is the **resource server**, not the authorization server. The only
discovery document it publishes is the RFC 9728 / MCP-draft protected-resource
metadata under ``/.well-known/oauth-protected-resource``. The RFC 8414
authorization-server metadata is owned by the IdP itself; Bernstein does not
attempt to fabricate it and never registers
``/.well-known/oauth-authorization-server``.

Covers:

  * the discovery helpers return ``None`` when no issuer is configured;
  * with an issuer set, the protected-resource metadata carries the
    configured authorization server and the resource URL;
  * the streamable HTTP transport serves the PR well-known path and falls
    back to 404 when discovery is off;
  * the AS well-known path is **not** registered (returns 404 even with an
    issuer set);
  * the capability card reports the discovery state.
"""

from __future__ import annotations

import asyncio
import json

import pytest

# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_MCP_OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_OAUTH_SCOPES", raising=False)


@pytest.fixture
def _with_issuer(monkeypatch: pytest.MonkeyPatch) -> str:
    issuer = "https://idp.example.com"
    monkeypatch.setenv("BERNSTEIN_MCP_OAUTH_ISSUER", issuer)
    monkeypatch.delenv("BERNSTEIN_MCP_OAUTH_SCOPES", raising=False)
    return issuer


def test_no_issuer_returns_none(_no_issuer: None) -> None:
    from bernstein.mcp.oauth import (
        oauth_discovery_enabled,
        protected_resource_metadata,
    )

    assert oauth_discovery_enabled() is False
    assert protected_resource_metadata("https://example.com/mcp") is None


def test_authorization_server_metadata_helper_removed() -> None:
    """The AS metadata builder must not exist; only the IdP can publish it."""
    import bernstein.mcp.oauth as oauth_mod

    assert not hasattr(oauth_mod, "authorization_server_metadata"), (
        "authorization_server_metadata was removed: Bernstein is the resource "
        "server and only the IdP can publish RFC 8414 metadata"
    )
    assert not hasattr(oauth_mod, "AS_METADATA_PATH"), (
        "AS_METADATA_PATH was removed alongside authorization_server_metadata"
    )


def test_protected_resource_metadata_carries_resource(_with_issuer: str) -> None:
    from bernstein.mcp.oauth import protected_resource_metadata

    meta = protected_resource_metadata("https://bernstein.example.com/mcp")
    assert meta is not None
    assert meta["resource"] == "https://bernstein.example.com/mcp"
    assert meta["authorization_servers"] == [_with_issuer]
    assert "header" in meta["bearer_methods_supported"]


def test_protected_resource_metadata_strips_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing slash on the issuer URL must be normalised."""
    from bernstein.mcp.oauth import protected_resource_metadata

    monkeypatch.setenv("BERNSTEIN_MCP_OAUTH_ISSUER", "https://idp.example.com/")
    meta = protected_resource_metadata("https://bernstein.example.com/mcp")
    assert meta is not None
    assert meta["authorization_servers"] == ["https://idp.example.com"]


def test_custom_scopes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.mcp.oauth import scopes_supported

    monkeypatch.setenv("BERNSTEIN_MCP_OAUTH_SCOPES", "foo, bar, baz")
    assert scopes_supported() == ["foo", "bar", "baz"]


# ---------------------------------------------------------------------------
# Streamable HTTP transport well-known paths
# ---------------------------------------------------------------------------


def test_transport_does_not_register_authorization_server_endpoint(
    _with_issuer: str,
) -> None:
    """The transport must not advertise an authorization-server endpoint.

    Bernstein cannot guess the IdP's path layout, so the AS well-known path
    is never served, even when an issuer is configured. Clients learn the
    AS endpoints from the IdP's own RFC 8414 metadata, which they reach by
    following ``authorization_servers[0]`` from the PR metadata.
    """
    from bernstein.mcp.remote_transport import (
        RemoteMCPConfig,
        StreamableHTTPTransport,
    )

    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg)
    status, _, _ = asyncio.run(
        transport.handle_request(
            "GET",
            "/.well-known/oauth-authorization-server",
            {"host": "bernstein.example.com"},
            b"",
        )
    )
    # Not registered: the path falls through to the standard 404 handler.
    assert status == 404


def test_transport_serves_protected_resource_metadata(_with_issuer: str) -> None:
    from bernstein.mcp.remote_transport import (
        RemoteMCPConfig,
        StreamableHTTPTransport,
    )

    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg)
    status, _, body = asyncio.run(
        transport.handle_request(
            "GET",
            "/.well-known/oauth-protected-resource",
            {"host": "bernstein.example.com", "x-forwarded-proto": "https"},
            b"",
        )
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["resource"] == "https://bernstein.example.com/mcp"
    assert payload["authorization_servers"] == [_with_issuer]


def test_transport_returns_404_for_protected_resource_when_discovery_disabled(
    _no_issuer: None,
) -> None:
    from bernstein.mcp.remote_transport import (
        RemoteMCPConfig,
        StreamableHTTPTransport,
    )

    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg)
    status, _, _ = asyncio.run(
        transport.handle_request(
            "GET",
            "/.well-known/oauth-protected-resource",
            {"host": "127.0.0.1"},
            b"",
        )
    )
    assert status == 404


def test_protected_resource_path_does_not_require_auth(_with_issuer: str) -> None:
    """A client probing discovery has no token yet; the path must not 401."""
    from bernstein.mcp.remote_transport import (
        RemoteMCPConfig,
        StreamableHTTPTransport,
    )

    # Bearer config that would 401 any /mcp request without Authorization.
    cfg = RemoteMCPConfig(
        host="127.0.0.1",
        auth_type="bearer",
        auth_token="secret",
    )
    transport = StreamableHTTPTransport(config=cfg)
    status, _, _ = asyncio.run(
        transport.handle_request(
            "GET",
            "/.well-known/oauth-protected-resource",
            {"host": "127.0.0.1"},
            b"",
        )
    )
    assert status == 200


# ---------------------------------------------------------------------------
# Capability card integration
# ---------------------------------------------------------------------------


def test_capability_card_reports_oauth_state(_with_issuer: str) -> None:
    from bernstein.mcp.capability import build_capability_card

    card = build_capability_card()
    oauth = card["auth"]["oauth"]
    assert oauth["enabled"] is True
    assert oauth["issuer"] == _with_issuer
    assert oauth["protectedResourceMetadata"] == "/.well-known/oauth-protected-resource"
    # The AS metadata path is no longer advertised.
    assert "authorizationServerMetadata" not in oauth
    # With discovery on, oauth2_pkce moves into supported.
    assert "oauth2_pkce" in card["auth"]["supported"]


def test_capability_card_reports_oauth_disabled(_no_issuer: None) -> None:
    from bernstein.mcp.capability import build_capability_card

    card = build_capability_card()
    assert card["auth"]["oauth"]["enabled"] is False
    # When off, oauth2_pkce remains planned, not supported.
    assert "oauth2_pkce" in card["auth"]["planned"]
    assert "oauth2_pkce" not in card["auth"]["supported"]


# ---------------------------------------------------------------------------
# WWW-Authenticate challenge helper (issue #3075)
# ---------------------------------------------------------------------------


def test_challenge_is_none_without_issuer(_no_issuer: None) -> None:
    from bernstein.mcp.oauth import www_authenticate_challenge

    assert www_authenticate_challenge("https://bernstein.example.com") is None


def test_challenge_points_at_the_metadata_path(_with_issuer: str) -> None:
    from bernstein.mcp.oauth import (
        PR_METADATA_PATH,
        protected_resource_metadata_url,
        www_authenticate_challenge,
    )

    base = "https://bernstein.example.com"
    url = protected_resource_metadata_url(base)
    assert url == f"{base}{PR_METADATA_PATH}"
    assert www_authenticate_challenge(base) == f'Bearer resource_metadata="{url}"'
    # The challenge names the document, never the issuer or any credential.
    assert _with_issuer not in www_authenticate_challenge(base)


def test_challenge_normalises_a_trailing_slash_on_the_base(_with_issuer: str) -> None:
    from bernstein.mcp.oauth import protected_resource_metadata_url

    assert protected_resource_metadata_url("https://bernstein.example.com/") == (
        "https://bernstein.example.com/.well-known/oauth-protected-resource"
    )
