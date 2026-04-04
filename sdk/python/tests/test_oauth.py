"""Tests for bernstein_sdk.oauth — OAuth 2.0 Authorization Code + PKCE."""

from __future__ import annotations

import base64
import hashlib
import urllib.parse
from unittest.mock import patch

import httpx
import pytest
import respx

from bernstein_sdk.oauth import (
    OAuthPKCEClient,
    PKCEChallenge,
    _extract_code,
)

# ---------------------------------------------------------------------------
# PKCEChallenge
# ---------------------------------------------------------------------------

AUTH_EP = "https://auth.example.com/authorize"
TOKEN_EP = "https://auth.example.com/token"
REDIRECT = "http://localhost:8080/callback"
CLIENT_ID = "test-client"

TOKEN_PAYLOAD = {
    "access_token": "at_abc123",
    "token_type": "Bearer",
    "expires_in": 3600,
    "refresh_token": "rt_xyz",
    "scope": "openid",
}


class TestPKCEChallenge:
    def test_verifier_length_is_128(self) -> None:
        c = PKCEChallenge.generate()
        assert len(c.code_verifier) == 128

    def test_verifier_is_url_safe(self) -> None:
        c = PKCEChallenge.generate()
        allowed = set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        assert all(ch in allowed for ch in c.code_verifier)

    def test_challenge_is_s256_of_verifier(self) -> None:
        c = PKCEChallenge.generate()
        digest = hashlib.sha256(c.code_verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert c.code_challenge == expected

    def test_challenge_method_is_s256(self) -> None:
        c = PKCEChallenge.generate()
        assert c.code_challenge_method == "S256"

    def test_each_generate_is_unique(self) -> None:
        a = PKCEChallenge.generate()
        b = PKCEChallenge.generate()
        assert a.code_verifier != b.code_verifier
        assert a.code_challenge != b.code_challenge


# ---------------------------------------------------------------------------
# OAuthPKCEClient.build_authorization_url
# ---------------------------------------------------------------------------

class TestBuildAuthorizationUrl:
    def _make_client(self) -> OAuthPKCEClient:
        return OAuthPKCEClient(
            client_id=CLIENT_ID,
            authorization_endpoint=AUTH_EP,
            token_endpoint=TOKEN_EP,
            redirect_uri=REDIRECT,
        )

    def test_url_contains_required_params(self) -> None:
        client = self._make_client()
        challenge = PKCEChallenge.generate()
        url, state = client.build_authorization_url(challenge)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

        assert qs["response_type"] == ["code"]
        assert qs["client_id"] == [CLIENT_ID]
        assert qs["redirect_uri"] == [REDIRECT]
        assert qs["code_challenge"] == [challenge.code_challenge]
        assert qs["code_challenge_method"] == ["S256"]

    def test_state_in_url_matches_returned_state(self) -> None:
        client = self._make_client()
        challenge = PKCEChallenge.generate()
        url, state = client.build_authorization_url(challenge)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert qs["state"] == [state]

    def test_each_call_produces_unique_state(self) -> None:
        client = self._make_client()
        challenge = PKCEChallenge.generate()
        _, state1 = client.build_authorization_url(challenge)
        _, state2 = client.build_authorization_url(challenge)
        assert state1 != state2

    def test_default_scope_is_openid(self) -> None:
        client = self._make_client()
        challenge = PKCEChallenge.generate()
        url, _ = client.build_authorization_url(challenge)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert qs["scope"] == ["openid"]

    def test_custom_scopes(self) -> None:
        client = OAuthPKCEClient(
            client_id=CLIENT_ID,
            authorization_endpoint=AUTH_EP,
            token_endpoint=TOKEN_EP,
            redirect_uri=REDIRECT,
            scopes=["openid", "profile", "email"],
        )
        challenge = PKCEChallenge.generate()
        url, _ = client.build_authorization_url(challenge)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert qs["scope"] == ["openid profile email"]


# ---------------------------------------------------------------------------
# OAuthPKCEClient.exchange_code
# ---------------------------------------------------------------------------

class TestExchangeCode:
    @respx.mock
    def test_exchange_sends_correct_form_body(self) -> None:
        route = respx.post(TOKEN_EP).mock(
            return_value=httpx.Response(200, json=TOKEN_PAYLOAD)
        )

        with OAuthPKCEClient(
            client_id=CLIENT_ID,
            authorization_endpoint=AUTH_EP,
            token_endpoint=TOKEN_EP,
            redirect_uri=REDIRECT,
        ) as client:
            tokens = client.exchange_code(
                code="auth_code_xyz", code_verifier="verifier_abc"
            )

        assert tokens.access_token == "at_abc123"
        assert tokens.token_type == "Bearer"
        assert tokens.expires_in == 3600
        assert tokens.refresh_token == "rt_xyz"

        body = urllib.parse.parse_qs(route.calls[0].request.content.decode())
        assert body["grant_type"] == ["authorization_code"]
        assert body["code"] == ["auth_code_xyz"]
        assert body["code_verifier"] == ["verifier_abc"]
        assert body["client_id"] == [CLIENT_ID]
        assert body["redirect_uri"] == [REDIRECT]

    @respx.mock
    def test_exchange_raises_on_4xx(self) -> None:
        respx.post(TOKEN_EP).mock(
            return_value=httpx.Response(401, json={"error": "invalid_client"})
        )
        with OAuthPKCEClient(
            client_id=CLIENT_ID,
            authorization_endpoint=AUTH_EP,
            token_endpoint=TOKEN_EP,
            redirect_uri=REDIRECT,
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                client.exchange_code(code="bad", code_verifier="bad")

    @respx.mock
    def test_token_response_raw_preserved(self) -> None:
        extra = {**TOKEN_PAYLOAD, "custom_field": "custom_value"}
        respx.post(TOKEN_EP).mock(
            return_value=httpx.Response(200, json=extra)
        )
        with OAuthPKCEClient(
            client_id=CLIENT_ID,
            authorization_endpoint=AUTH_EP,
            token_endpoint=TOKEN_EP,
            redirect_uri=REDIRECT,
        ) as client:
            tokens = client.exchange_code(code="c", code_verifier="v")
        assert tokens.raw["custom_field"] == "custom_value"


# ---------------------------------------------------------------------------
# Full flow (authorize — manual mode)
# ---------------------------------------------------------------------------

class TestFullFlow:
    @respx.mock
    def test_manual_flow_produces_valid_tokens(self) -> None:
        """Full OAuth PKCE flow: generate challenge → build URL → exchange code."""
        respx.post(TOKEN_EP).mock(
            return_value=httpx.Response(200, json=TOKEN_PAYLOAD)
        )

        client = OAuthPKCEClient(
            client_id=CLIENT_ID,
            authorization_endpoint=AUTH_EP,
            token_endpoint=TOKEN_EP,
            redirect_uri=REDIRECT,
        )
        challenge = PKCEChallenge.generate()
        url, _ = client.build_authorization_url(challenge)

        # Simulate user visiting url and receiving code in redirect
        tokens = client.exchange_code(
            code="simulated_auth_code",
            code_verifier=challenge.code_verifier,
        )
        assert tokens.access_token == "at_abc123"
        assert tokens.token_type == "Bearer"

    @respx.mock
    def test_authorize_manual_mode(self, capsys: pytest.CaptureFixture[str]) -> None:
        respx.post(TOKEN_EP).mock(
            return_value=httpx.Response(200, json=TOKEN_PAYLOAD)
        )
        with (
            patch("builtins.input", return_value="simulated_code"),
            OAuthPKCEClient(
                client_id=CLIENT_ID,
                authorization_endpoint=AUTH_EP,
                token_endpoint=TOKEN_EP,
                redirect_uri=REDIRECT,
            ) as client,
        ):
            tokens = client.authorize(auto=False)

        assert tokens.access_token == "at_abc123"
        captured = capsys.readouterr()
        assert AUTH_EP in captured.out

    @respx.mock
    def test_authorize_auto_mode_opens_browser(self) -> None:
        respx.post(TOKEN_EP).mock(
            return_value=httpx.Response(200, json=TOKEN_PAYLOAD)
        )
        with (
            patch("webbrowser.open") as mock_browser,
            patch("builtins.input", return_value="simulated_code"),
            OAuthPKCEClient(
                client_id=CLIENT_ID,
                authorization_endpoint=AUTH_EP,
                token_endpoint=TOKEN_EP,
                redirect_uri=REDIRECT,
            ) as client,
        ):
            tokens = client.authorize(auto=True)

        mock_browser.assert_called_once()
        assert tokens.access_token == "at_abc123"


# ---------------------------------------------------------------------------
# _extract_code helper
# ---------------------------------------------------------------------------

class TestExtractCode:
    def test_raw_code_returned_as_is(self) -> None:
        assert _extract_code("abc123") == "abc123"

    def test_code_extracted_from_full_redirect_url(self) -> None:
        url = "http://localhost:8080/callback?code=mycode&state=s"
        assert _extract_code(url) == "mycode"

    def test_raises_when_no_code_in_url(self) -> None:
        with pytest.raises(ValueError, match="No 'code' parameter"):
            _extract_code("http://localhost:8080/callback?error=access_denied")
