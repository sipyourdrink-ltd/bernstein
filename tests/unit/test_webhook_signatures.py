"""Tests for shared webhook HMAC-SHA256 verification."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from bernstein.core.webhook_signatures import sign_hmac_sha256, verify_hmac_sha256
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app
from bernstein.core.trigger_sources.slack import verify_slack_signature
from bernstein.github_app.webhooks import verify_signature


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Return a temporary JSONL path."""

    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path) -> FastAPI:
    """Create a test app."""

    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Create an async test client."""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def test_verify_hmac_sha256_accepts_valid_sha256_signature() -> None:
    """Shared verifier should accept a valid prefixed SHA-256 HMAC."""

    body = b'{"ok":true}'
    signature = sign_hmac_sha256("top-secret", body, prefix="sha256=")

    assert verify_hmac_sha256(body, signature, "top-secret", prefix="sha256=") is True


def test_verify_hmac_sha256_rejects_bad_prefix_and_malformed_hex() -> None:
    """Shared verifier should reject malformed prefixes and invalid hex digests."""

    body = b'{"ok":true}'

    assert verify_hmac_sha256(body, "sha1=deadbeef", "top-secret", prefix="sha256=") is False
    assert verify_hmac_sha256(body, "sha256=not-hex", "top-secret", prefix="sha256=") is False


def test_github_verify_signature_uses_shared_hmac_rules() -> None:
    """GitHub verification should reject malformed values and accept valid signatures."""

    body = json.dumps({"action": "opened"}).encode("utf-8")
    signature = sign_hmac_sha256("gh-secret", body, prefix="sha256=")

    assert verify_signature(body, signature, "gh-secret") is True
    assert verify_signature(body, "sha256=xyz", "gh-secret") is False


def test_slack_verify_signature_remains_valid_with_shared_hmac_helper() -> None:
    """Slack verification should remain valid after sharing the HMAC implementation."""

    body = b"token=abc&team_id=T1"
    timestamp = str(int(time.time()))
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}".encode()
    signature = sign_hmac_sha256("slack-secret", basestring, prefix="v0=")

    assert verify_slack_signature(body, timestamp, signature, "slack-secret") is True


@pytest.mark.anyio
async def test_generic_webhook_accepts_hmac_sha256_signature(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generic webhook should accept a valid HMAC-SHA256 signature header (audit-121)."""

    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", "top-secret")
    payload = {"title": "HMAC allowed", "description": "Signed payload."}
    body = json.dumps(payload).encode("utf-8")
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode() + body
    signature = sign_hmac_sha256("top-secret", signed_payload, prefix="sha256=")

    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-bernstein-timestamp": str(timestamp),
            "x-bernstein-webhook-signature-256": signature,
        },
    )

    assert response.status_code == 201
    assert response.json()["task"]["title"] == "HMAC allowed"


@pytest.mark.anyio
async def test_generic_webhook_rejects_bad_hmac_sha256_signature(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generic webhook should reject bad HMAC-SHA256 signatures when a secret is configured."""

    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", "top-secret")
    payload = {"title": "Denied", "description": "Bad signature."}
    timestamp = int(time.time())

    response = await client.post(
        "/webhook",
        content=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-bernstein-timestamp": str(timestamp),
            "x-bernstein-webhook-signature-256": "sha256=deadbeef",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid webhook signature"
