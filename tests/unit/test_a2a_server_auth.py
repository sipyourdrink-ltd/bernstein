"""API-key + OAuth2 client-credentials auth for the A2A server surface (#2609).

The binding design directive requires the callable node to accept two auth
schemes declared in its agent card - a static API key and an OAuth2
client-credentials grant - to reject missing or invalid credentials per the
A2A spec, and to name the authenticated caller so the accepting path can
anchor it in the audit chain.

These tests exercise the pure authenticator: no HTTP, stdlib crypto only.
"""

from __future__ import annotations

import hashlib

import pytest

from bernstein.core.protocols.a2a.server_auth import (
    A2AAuthError,
    A2AServerAuth,
    _derive_signing_key,
)


def _auth() -> A2AServerAuth:
    return A2AServerAuth(
        api_keys={"alice": "key-alice", "bob": "key-bob"},
        oauth_clients={"client-x": "secret-x"},
        signing_secret=b"unit-test-secret",
    )


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------


def test_valid_api_key_authenticates_the_named_caller() -> None:
    caller = _auth().authenticate({"x-api-key": "key-alice"})
    assert caller.caller_id == "alice"
    assert caller.scheme == "apiKey"


def test_unknown_api_key_is_rejected() -> None:
    with pytest.raises(A2AAuthError) as exc:
        _auth().authenticate({"x-api-key": "not-a-key"})
    assert exc.value.status_code == 401


def test_missing_credentials_are_rejected_with_a_challenge() -> None:
    with pytest.raises(A2AAuthError) as exc:
        _auth().authenticate({})
    assert exc.value.status_code == 401
    # RFC 6750: an unauthenticated request gets a WWW-Authenticate challenge.
    assert "www-authenticate" in {k.lower() for k in exc.value.headers}


# ---------------------------------------------------------------------------
# OAuth2 client-credentials
# ---------------------------------------------------------------------------


def test_client_credentials_grant_issues_a_bearer_token() -> None:
    auth = _auth()
    token = auth.issue_client_credentials_token(
        client_id="client-x",
        client_secret="secret-x",
        now=1000.0,
    )
    assert token.token_type == "Bearer"
    assert token.expires_in > 0
    assert token.access_token


def test_bad_client_secret_is_refused() -> None:
    with pytest.raises(A2AAuthError) as exc:
        _auth().issue_client_credentials_token(
            client_id="client-x",
            client_secret="wrong",
            now=1000.0,
        )
    # OAuth2 §5.2 invalid_client.
    assert exc.value.error == "invalid_client"


def test_issued_token_authenticates_the_client_as_caller() -> None:
    auth = _auth()
    token = auth.issue_client_credentials_token(
        client_id="client-x",
        client_secret="secret-x",
        now=1000.0,
    )
    caller = auth.authenticate(
        {"authorization": f"Bearer {token.access_token}"},
        now=1000.0,
    )
    assert caller.caller_id == "client-x"
    assert caller.scheme == "oauth2"


def test_expired_token_is_rejected() -> None:
    auth = _auth()
    token = auth.issue_client_credentials_token(
        client_id="client-x",
        client_secret="secret-x",
        now=1000.0,
    )
    with pytest.raises(A2AAuthError) as exc:
        auth.authenticate(
            {"authorization": f"Bearer {token.access_token}"},
            now=1000.0 + token.expires_in + 1,
        )
    assert exc.value.status_code == 401


def test_tampered_token_is_rejected() -> None:
    auth = _auth()
    token = auth.issue_client_credentials_token(
        client_id="client-x",
        client_secret="secret-x",
        now=1000.0,
    )
    forged = token.access_token[:-4] + ("AAAA" if not token.access_token.endswith("AAAA") else "BBBB")
    with pytest.raises(A2AAuthError):
        auth.authenticate({"authorization": f"Bearer {forged}"}, now=1000.0)


def test_token_signed_by_a_different_secret_is_rejected() -> None:
    issuer = _auth()
    token = issuer.issue_client_credentials_token(
        client_id="client-x",
        client_secret="secret-x",
        now=1000.0,
    )
    other = A2AServerAuth(
        api_keys={},
        oauth_clients={"client-x": "secret-x"},
        signing_secret=b"a-different-secret",
    )
    with pytest.raises(A2AAuthError):
        other.authenticate({"authorization": f"Bearer {token.access_token}"}, now=1000.0)


# ---------------------------------------------------------------------------
# Card advertisement
# ---------------------------------------------------------------------------


def test_security_schemes_declare_both_mechanisms() -> None:
    schemes = _auth().security_schemes(token_url="https://node.example/a2a/v1/oauth/token")
    by_id = {s["id"]: s for s in schemes}
    assert by_id["a2a-api-key"]["type"] == "apiKey"
    assert by_id["a2a-api-key"]["in"] == "header"
    assert by_id["a2a-api-key"]["name"].lower() == "x-api-key"
    oauth = by_id["a2a-oauth2"]
    assert oauth["type"] == "oauth2"
    cc = oauth["flows"]["clientCredentials"]
    assert cc["tokenUrl"] == "https://node.example/a2a/v1/oauth/token"


# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------


def test_from_env_parses_api_keys_and_oauth_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_A2A_API_KEYS", "alice=key-alice, bob=key-bob")
    monkeypatch.setenv("BERNSTEIN_A2A_OAUTH_CLIENTS", "client-x=secret-x")
    monkeypatch.delenv("BERNSTEIN_A2A_OAUTH_SIGNING_SECRET", raising=False)
    auth = A2AServerAuth.from_env()
    assert auth.authenticate({"x-api-key": "key-alice"}).caller_id == "alice"
    # Signing secret is derived deterministically from the client set when
    # unset, so a rebuilt authenticator validates the same tokens.
    token = auth.issue_client_credentials_token(client_id="client-x", client_secret="secret-x", now=5.0)
    rebuilt = A2AServerAuth.from_env()
    assert rebuilt.authenticate({"authorization": f"Bearer {token.access_token}"}, now=5.0).caller_id == "client-x"


def test_from_env_with_no_config_authenticates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_A2A_API_KEYS", raising=False)
    monkeypatch.delenv("BERNSTEIN_A2A_OAUTH_CLIENTS", raising=False)
    auth = A2AServerAuth.from_env()
    assert not auth.is_configured
    with pytest.raises(A2AAuthError):
        auth.authenticate({"x-api-key": "anything"})


# --------------------------------------------------------------------------- #
# signing-key derivation
# --------------------------------------------------------------------------- #


def test_derived_key_is_stable_across_authenticators() -> None:
    """A restart must still validate tokens issued before it.

    The derivation exists so two processes behind one config agree on the
    signing key without persisting a secret; if this stops holding, every
    restart silently invalidates live tokens.
    """
    clients = {"svc-a": "secret-a", "svc-b": "secret-b"}

    first = A2AServerAuth(oauth_clients=dict(clients))
    second = A2AServerAuth(oauth_clients=dict(clients))

    assert first.signing_secret == second.signing_secret

    token = first.issue_client_credentials_token(client_id="svc-a", client_secret="secret-a", now=1000.0).access_token
    caller = second.authenticate({"Authorization": f"Bearer {token}"}, now=1001.0)
    assert caller.caller_id == "svc-a"


def test_a_changed_client_set_derives_a_different_key() -> None:
    """Token validity is bound to the configured clients, not just the secret."""
    one = A2AServerAuth(oauth_clients={"svc-a": "secret-a"})
    two = A2AServerAuth(oauth_clients={"svc-a": "secret-a", "svc-b": "secret-b"})

    assert one.signing_secret != two.signing_secret


def test_client_secrets_are_not_recoverable_at_hardware_speed() -> None:
    """The derived key must not be one fast hash away from the secrets.

    Whoever holds the signing key can already forge tokens; what stretching
    protects is the operator's client secrets, which are short and reused
    elsewhere. A single SHA-256 pass over the material would let a leaked key
    be ground back into them offline. This asserts the cheap construction is
    not what ships.
    """
    material = "svc-a=secret-a"
    cheap = hashlib.sha256(b"bernstein-a2a-oauth\x00" + material.encode("utf-8")).digest()

    shipped = A2AServerAuth(oauth_clients={"svc-a": "secret-a"}).signing_secret

    assert shipped != cheap
    assert len(shipped) == 32


def test_derivation_is_not_repaid_on_every_request() -> None:
    """`_get_auth` builds an authenticator per request when state has none.

    An un-memoised 600k-iteration KDF would therefore land on every A2A
    request rather than on startup, so the cache is load-bearing rather than
    an optimisation.
    """
    clients = {"svc-cache": "secret-cache"}
    _derive_signing_key.cache_clear()

    A2AServerAuth(oauth_clients=dict(clients))
    after_first = _derive_signing_key.cache_info()
    A2AServerAuth(oauth_clients=dict(clients))
    after_second = _derive_signing_key.cache_info()

    assert after_first.misses == 1
    assert after_second.misses == 1, "a second authenticator re-ran the KDF"
    assert after_second.hits == after_first.hits + 1
