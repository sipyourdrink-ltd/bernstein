"""Tests for the outbound-signing key directory well-known route.

Covers issue #2305 AC1: the key directory a verifier fetches to validate
outbound HTTP Message Signatures is published at a ``.well-known`` path and
carries the install-identity public key as a JWK whose ``kid`` is the
install-identity thumbprint used as the signature ``keyid``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bernstein.core.identity import http_signing
from bernstein.core.routes.well_known import (
    HTTP_SIG_DIRECTORY_PATH,
    _reset_signing_keypair_for_tests,
)
from bernstein.core.security.agent_card_keystore import AgentCardKeystore
from bernstein.core.security.auth_middleware import AUTH_PUBLIC_PATHS
from bernstein.core.server import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    os.environ["BERNSTEIN_AUTH_DISABLED"] = "1"
    os.environ["BERNSTEIN_AGENT_CARD_KEY_DIR"] = str(tmp_path / "keys")
    _reset_signing_keypair_for_tests(tmp_path / "keys")
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    yield TestClient(app)
    os.environ.pop("BERNSTEIN_AGENT_CARD_KEY_DIR", None)


def test_directory_path_is_public(client: TestClient) -> None:
    assert HTTP_SIG_DIRECTORY_PATH in AUTH_PUBLIC_PATHS


def test_directory_publishes_install_identity_jwk(client: TestClient, tmp_path: Path) -> None:
    resp = client.get(HTTP_SIG_DIRECTORY_PATH)
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("keys")
    jwk = body["keys"][0]
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    _priv, pub = AgentCardKeystore(tmp_path / "keys").load_or_generate()
    assert jwk["kid"] == http_signing.install_identity_keyid(pub)


def test_directory_verifies_a_signed_request(client: TestClient, tmp_path: Path) -> None:
    keystore = AgentCardKeystore(tmp_path / "keys")
    headers = http_signing.sign_request(method="GET", url="https://peer.example/x", headers={}, keystore=keystore)
    keydir = client.get(HTTP_SIG_DIRECTORY_PATH).json()
    assert http_signing.verify_request(
        method="GET", url="https://peer.example/x", headers=headers, key_directory=keydir
    )
