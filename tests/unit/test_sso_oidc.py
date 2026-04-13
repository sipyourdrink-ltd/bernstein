"""Tests for ENT-006: SSO/OIDC authentication for web dashboard."""

from __future__ import annotations

import time

import pytest
from bernstein.core.sso_oidc import (
    OIDCConfig,
    OIDCDiscoveryDocument,
    OIDCProvider,
    OIDCTokenResponse,
    parse_discovery_document,
    parse_token_response,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(**overrides: object) -> OIDCConfig:
    defaults: dict[str, object] = {
        "issuer_url": "https://idp.example.com",
        "client_id": "bernstein-test",
        "client_secret": "test-secret",
        "redirect_uri": "http://localhost:8052/auth/callback",
    }
    defaults.update(overrides)
    return OIDCConfig(**defaults)  # type: ignore[arg-type]


def _make_discovery() -> OIDCDiscoveryDocument:
    return OIDCDiscoveryDocument(
        issuer="https://idp.example.com",
        authorization_endpoint="https://idp.example.com/oauth/authorize",
        token_endpoint="https://idp.example.com/oauth/token",
        userinfo_endpoint="https://idp.example.com/userinfo",
        jwks_uri="https://idp.example.com/.well-known/jwks.json",
        end_session_endpoint="https://idp.example.com/logout",
    )


def _make_tokens() -> OIDCTokenResponse:
    return OIDCTokenResponse(
        access_token="access-abc",
        id_token="id-token-xyz",
        refresh_token="refresh-123",
        expires_at=time.time() + 3600,
        token_type="Bearer",
        scope="openid profile email",
    )


# ---------------------------------------------------------------------------
# Discovery document parsing
# ---------------------------------------------------------------------------


class TestParseDiscoveryDocument:
    def test_parses_all_fields(self) -> None:
        data = {
            "issuer": "https://idp.example.com",
            "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
            "jwks_uri": "https://idp.example.com/jwks",
            "end_session_endpoint": "https://idp.example.com/logout",
        }
        doc = parse_discovery_document(data)
        assert doc.issuer == "https://idp.example.com"
        assert doc.authorization_endpoint == "https://idp.example.com/authorize"
        assert doc.token_endpoint == "https://idp.example.com/token"

    def test_handles_missing_fields(self) -> None:
        doc = parse_discovery_document({})
        assert doc.issuer == ""
        assert doc.authorization_endpoint == ""


# ---------------------------------------------------------------------------
# Token response parsing
# ---------------------------------------------------------------------------


class TestParseTokenResponse:
    def test_parses_standard_response(self) -> None:
        data = {
            "access_token": "abc",
            "id_token": "xyz",
            "refresh_token": "refresh",
            "expires_in": 7200,
            "token_type": "Bearer",
            "scope": "openid",
        }
        tokens = parse_token_response(data)
        assert tokens.access_token == "abc"
        assert tokens.id_token == "xyz"
        assert tokens.refresh_token == "refresh"
        assert tokens.expires_at > time.time()

    def test_default_expiry(self) -> None:
        tokens = parse_token_response({"access_token": "t"})
        assert tokens.expires_at > time.time()


# ---------------------------------------------------------------------------
# OIDCProvider: authorization URL
# ---------------------------------------------------------------------------


class TestOIDCProviderAuthUrl:
    def test_builds_url_with_all_params(self) -> None:
        config = _make_config()
        provider = OIDCProvider(config, discovery=_make_discovery())
        url = provider.authorization_url(state="test-state")
        assert "response_type=code" in url
        assert "client_id=bernstein-test" in url
        assert "state=test-state" in url
        assert url.startswith("https://idp.example.com/oauth/authorize?")

    def test_auto_generates_state(self) -> None:
        provider = OIDCProvider(_make_config(), discovery=_make_discovery())
        url = provider.authorization_url()
        assert "state=" in url

    def test_raises_without_discovery(self) -> None:
        provider = OIDCProvider(_make_config())
        with pytest.raises(ValueError, match="Discovery document"):
            provider.authorization_url()

    def test_includes_audience_when_set(self) -> None:
        config = _make_config(audience="https://api.example.com")
        provider = OIDCProvider(config, discovery=_make_discovery())
        url = provider.authorization_url(state="s")
        assert "audience=" in url


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


class TestStateManagement:
    def test_generate_and_validate(self) -> None:
        provider = OIDCProvider(_make_config())
        state = provider.generate_state()
        assert len(state) > 0
        assert provider.validate_state(state)

    def test_state_consumed_once(self) -> None:
        provider = OIDCProvider(_make_config())
        state = provider.generate_state()
        assert provider.validate_state(state)
        assert not provider.validate_state(state)

    def test_invalid_state_rejected(self) -> None:
        provider = OIDCProvider(_make_config())
        assert not provider.validate_state("unknown-state")


# ---------------------------------------------------------------------------
# Role mapping
# ---------------------------------------------------------------------------


class TestRoleMapping:
    def test_maps_group_to_role(self) -> None:
        config = _make_config(
            role_mapping={
                "eng-admins": "admin",
                "eng-team": "operator",
            }
        )
        provider = OIDCProvider(config)
        assert provider.resolve_role(["eng-admins"]) == "admin"
        assert provider.resolve_role(["eng-team"]) == "operator"

    def test_first_match_wins(self) -> None:
        config = _make_config(
            role_mapping={
                "admin-group": "admin",
                "ops-group": "operator",
            }
        )
        provider = OIDCProvider(config)
        assert provider.resolve_role(["admin-group", "ops-group"]) == "admin"

    def test_defaults_to_viewer(self) -> None:
        provider = OIDCProvider(_make_config())
        assert provider.resolve_role(["unknown-group"]) == "viewer"
        assert provider.resolve_role([]) == "viewer"


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


class TestSessionManagement:
    def test_create_and_get(self) -> None:
        provider = OIDCProvider(_make_config())
        tokens = _make_tokens()
        session = provider.create_session(
            tokens=tokens,
            subject="user-123",
            email="user@example.com",
            groups=[],
        )
        assert session.session_id
        assert session.email == "user@example.com"

        fetched = provider.get_session(session.session_id)
        assert fetched is not None
        assert fetched.session_id == session.session_id

    def test_expired_session_returns_none(self) -> None:
        config = _make_config(session_timeout_s=0)
        provider = OIDCProvider(config)
        session = provider.create_session(
            tokens=_make_tokens(),
            subject="u",
            email="u@e.com",
            groups=[],
        )
        assert provider.get_session(session.session_id) is None

    def test_revoke_session(self) -> None:
        provider = OIDCProvider(_make_config())
        session = provider.create_session(
            tokens=_make_tokens(),
            subject="u",
            email="u@e.com",
            groups=[],
        )
        assert provider.revoke_session(session.session_id)
        assert provider.get_session(session.session_id) is None

    def test_revoke_nonexistent(self) -> None:
        provider = OIDCProvider(_make_config())
        assert not provider.revoke_session("nonexistent")

    def test_active_session_count(self) -> None:
        provider = OIDCProvider(_make_config())
        for i in range(3):
            provider.create_session(
                tokens=_make_tokens(),
                subject=f"u{i}",
                email=f"u{i}@e.com",
                groups=[],
            )
        assert provider.active_session_count() == 3


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    def test_needs_refresh_within_margin(self) -> None:
        config = _make_config(token_refresh_margin_s=600)
        provider = OIDCProvider(config)
        tokens = OIDCTokenResponse(
            access_token="a",
            expires_at=time.time() + 300,  # 5 min left, margin is 10 min
        )
        session = provider.create_session(
            tokens=tokens,
            subject="u",
            email="u@e.com",
            groups=[],
        )
        assert provider.needs_token_refresh(session)

    def test_no_refresh_outside_margin(self) -> None:
        config = _make_config(token_refresh_margin_s=300)
        provider = OIDCProvider(config)
        tokens = OIDCTokenResponse(
            access_token="a",
            expires_at=time.time() + 3600,  # 60 min left
        )
        session = provider.create_session(
            tokens=tokens,
            subject="u",
            email="u@e.com",
            groups=[],
        )
        assert not provider.needs_token_refresh(session)

    def test_update_tokens(self) -> None:
        provider = OIDCProvider(_make_config())
        session = provider.create_session(
            tokens=_make_tokens(),
            subject="u",
            email="u@e.com",
            groups=[],
        )
        new_tokens = OIDCTokenResponse(
            access_token="new-access",
            expires_at=time.time() + 7200,
        )
        assert provider.update_tokens(session.session_id, new_tokens)
        refreshed = provider.get_session(session.session_id)
        assert refreshed is not None
        assert refreshed.tokens.access_token == "new-access"


# ---------------------------------------------------------------------------
# Discovery & token request builders
# ---------------------------------------------------------------------------


class TestRequestBuilders:
    def test_discovery_url(self) -> None:
        config = _make_config(issuer_url="https://idp.example.com/")
        provider = OIDCProvider(config)
        url = provider.build_discovery_url()
        assert url == "https://idp.example.com/.well-known/openid-configuration"

    def test_token_request_body(self) -> None:
        provider = OIDCProvider(_make_config())
        body = provider.build_token_request_body("auth-code-xyz")
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "auth-code-xyz"
        assert body["client_id"] == "bernstein-test"
        assert body["client_secret"] == "test-secret"

    def test_refresh_request_body(self) -> None:
        provider = OIDCProvider(_make_config())
        body = provider.build_refresh_request_body("refresh-tok")
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "refresh-tok"
