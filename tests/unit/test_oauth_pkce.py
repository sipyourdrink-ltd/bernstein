"""Unit tests for OAuth 2.0 PKCE flow (bernstein.core.oauth_pkce)."""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import patch

import httpx
import pytest
import respx

from bernstein.core.oauth_pkce import (
    OAuthError,
    PKCEFlow,
    PKCETokens,
    generate_code_challenge,
    generate_code_verifier,
    generate_pkce_pair,
)

# ---------------------------------------------------------------------------
# PKCE primitive tests
# ---------------------------------------------------------------------------


class TestGenerateCodeVerifier:
    def test_length_is_128(self) -> None:
        verifier = generate_code_verifier()
        assert len(verifier) == 128

    def test_url_safe_characters_only(self) -> None:
        verifier = generate_code_verifier()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        for ch in verifier:
            assert ch in allowed, f"Unexpected char: {ch!r}"

    def test_is_random(self) -> None:
        v1 = generate_code_verifier()
        v2 = generate_code_verifier()
        assert v1 != v2


class TestGenerateCodeChallenge:
    def test_s256_derivation(self) -> None:
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        )
        assert generate_code_challenge(verifier) == expected

    def test_no_padding(self) -> None:
        verifier = generate_code_verifier()
        challenge = generate_code_challenge(verifier)
        assert "=" not in challenge

    def test_deterministic(self) -> None:
        verifier = generate_code_verifier()
        assert generate_code_challenge(verifier) == generate_code_challenge(verifier)


class TestGeneratePkcePair:
    def test_returns_two_strings(self) -> None:
        verifier, challenge = generate_pkce_pair()
        assert isinstance(verifier, str)
        assert isinstance(challenge, str)

    def test_challenge_matches_verifier(self) -> None:
        verifier, challenge = generate_pkce_pair()
        expected = generate_code_challenge(verifier)
        assert challenge == expected


# ---------------------------------------------------------------------------
# PKCEFlow tests
# ---------------------------------------------------------------------------

AUTH_ENDPOINT = "https://idp.example.com/oauth/authorize"
TOKEN_ENDPOINT = "https://idp.example.com/oauth/token"
REDIRECT_URI = "http://localhost:8099/callback"
CLIENT_ID = "test-client"

TOKEN_RESPONSE = {
    "access_token": "acc-tok-xyz",
    "token_type": "Bearer",
    "expires_in": 3600,
    "refresh_token": "ref-tok-abc",
    "id_token": "id-tok-def",
    "scope": "openid profile email",
}


def _make_flow(**kwargs: str) -> PKCEFlow:
    return PKCEFlow(
        client_id=CLIENT_ID,
        authorization_endpoint=AUTH_ENDPOINT,
        token_endpoint=TOKEN_ENDPOINT,
        redirect_uri=REDIRECT_URI,
        **kwargs,  # type: ignore[arg-type]
    )


class TestPKCEFlowAuthUrl:
    def test_url_contains_code_challenge(self) -> None:
        flow = _make_flow()
        url = flow.get_authorization_url()
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url

    def test_url_contains_client_id(self) -> None:
        flow = _make_flow()
        url = flow.get_authorization_url()
        assert f"client_id={CLIENT_ID}" in url

    def test_url_contains_redirect_uri(self) -> None:
        flow = _make_flow()
        url = flow.get_authorization_url()
        assert "redirect_uri=" in url

    def test_url_contains_state(self) -> None:
        flow = _make_flow()
        url = flow.get_authorization_url()
        assert "state=" in url

    def test_url_starts_with_auth_endpoint(self) -> None:
        flow = _make_flow()
        url = flow.get_authorization_url()
        assert url.startswith(AUTH_ENDPOINT)

    def test_challenge_in_url_matches_verifier(self) -> None:
        flow = _make_flow()
        flow.start()
        verifier = flow._code_verifier
        url = flow.get_authorization_url()
        from urllib.parse import parse_qs, urlparse

        params = parse_qs(urlparse(url).query)
        challenge_in_url = params["code_challenge"][0]
        assert challenge_in_url == generate_code_challenge(verifier)


class TestPKCEFlowExchangeCode:
    @pytest.mark.asyncio
    @respx.mock
    async def test_successful_exchange_returns_tokens(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
        flow = _make_flow()
        flow.start()
        tokens = await flow.exchange_code("auth-code-123")

        assert tokens.access_token == "acc-tok-xyz"
        assert tokens.refresh_token == "ref-tok-abc"
        assert tokens.id_token == "id-tok-def"
        assert tokens.expires_in == 3600

    @pytest.mark.asyncio
    @respx.mock
    async def test_code_verifier_sent_in_request(self) -> None:
        route = respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
        flow = _make_flow()
        flow.start()
        verifier = flow._code_verifier

        await flow.exchange_code("auth-code-456")

        body = route.calls[0].request.content.decode()
        assert f"code_verifier={verifier}" in body

    @pytest.mark.asyncio
    @respx.mock
    async def test_raises_oauth_error_on_http_failure(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        flow = _make_flow()
        flow.start()

        with pytest.raises(OAuthError, match="Token exchange failed"):
            await flow.exchange_code("bad-code")

    @pytest.mark.asyncio
    async def test_raises_value_error_if_not_started(self) -> None:
        flow = _make_flow()
        with pytest.raises(ValueError, match="PKCE flow not started"):
            await flow.exchange_code("any-code")

    @pytest.mark.asyncio
    @respx.mock
    async def test_raises_oauth_error_if_access_token_missing(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(200, json={"token_type": "Bearer"}))
        flow = _make_flow()
        flow.start()

        with pytest.raises(OAuthError, match="missing access_token"):
            await flow.exchange_code("auth-code-789")


class TestPKCEFlowManualMode:
    @pytest.mark.asyncio
    @respx.mock
    async def test_run_manual_returns_tokens(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
        flow = _make_flow()
        flow.start()
        tokens = await flow.run_manual("manual-code-abc")

        assert tokens.access_token == "acc-tok-xyz"

    @pytest.mark.asyncio
    @respx.mock
    async def test_run_manual_starts_flow_if_not_started(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
        flow = _make_flow()
        # No explicit start()
        tokens = await flow.run_manual("manual-code-xyz")
        assert tokens.access_token == "acc-tok-xyz"


class TestPKCEFlowAutomaticMode:
    @pytest.mark.asyncio
    @respx.mock
    async def test_run_automatic_exchanges_captured_code(self) -> None:
        respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
        flow = _make_flow()

        with patch("bernstein.core.security.oauth_pkce._wait_for_callback", return_value="auto-code-000"):
            tokens = await flow.run_automatic(open_browser=False)

        assert tokens.access_token == "acc-tok-xyz"

    @pytest.mark.asyncio
    async def test_run_automatic_raises_on_no_code(self) -> None:
        flow = _make_flow()

        with patch("bernstein.core.security.oauth_pkce._wait_for_callback", return_value=None):
            with pytest.raises(OAuthError, match="No authorization code received"):
                await flow.run_automatic(open_browser=False)


class TestPKCETokens:
    def test_from_response_parses_all_fields(self) -> None:
        tokens = PKCETokens.from_response(TOKEN_RESPONSE)
        assert tokens.access_token == "acc-tok-xyz"
        assert tokens.refresh_token == "ref-tok-abc"
        assert tokens.id_token == "id-tok-def"
        assert tokens.expires_in == 3600
        assert tokens.scope == "openid profile email"

    def test_raw_field_preserved(self) -> None:
        tokens = PKCETokens.from_response(TOKEN_RESPONSE)
        assert tokens.raw == TOKEN_RESPONSE
