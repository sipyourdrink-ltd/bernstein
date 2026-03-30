"""Tests for Slack webhook routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path):
    """App without a signing secret — signature verification disabled."""
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
def app_with_secret(jsonl_path: Path):
    """App with a signing secret — all requests must have valid signatures."""
    return create_app(jsonl_path=jsonl_path, slack_signing_secret="test_secret_key")


@pytest.fixture()
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
async def client_with_secret(app_with_secret) -> AsyncClient:
    transport = ASGITransport(app=app_with_secret)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _slack_sig_headers(body: bytes, secret: str) -> dict[str, str]:
    """Build valid X-Slack-* signature headers for the given body and secret."""
    timestamp = str(int(time.time()))
    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    signature = (
        "v0="
        + hmac.new(
            secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )
    return {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
    }


# ---------------------------------------------------------------------------
# Test: slash command creates task with correct slack_context
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_slash_command_creates_task_with_slack_context(client: AsyncClient) -> None:
    """POST /webhooks/slack/commands creates a task and returns the task ID."""
    form_body = (
        b"command=%2Fbernstein"
        b"&text=fix+the+login+bug"
        b"&user_id=U123ABC"
        b"&channel_id=C456DEF"
        b"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2Fresponse"
        b"&trigger_id=T789GHI"
    )
    resp = await client.post(
        "/webhooks/slack/commands",
        content=form_body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["response_type"] == "ephemeral"
    # Response must reference the created task ID
    assert "fix the login bug" in data["text"] or data["text"].startswith("Task `")
    # Verify the task was stored with correct slack_context
    tasks_resp = await client.get("/tasks?status=open")
    assert tasks_resp.status_code == 200
    tasks = tasks_resp.json()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["slack_context"]["channel_id"] == "C456DEF"
    assert task["slack_context"]["user_id"] == "U123ABC"


# ---------------------------------------------------------------------------
# Test: events endpoint handles url_verification challenge
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_events_url_verification_challenge(client: AsyncClient) -> None:
    """POST /webhooks/slack/events returns the challenge for url_verification."""
    challenge_token = "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P"
    payload = {
        "type": "url_verification",
        "challenge": challenge_token,
        "token": "fake_verification_token",
    }
    resp = await client.post(
        "/webhooks/slack/events",
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["challenge"] == challenge_token


# ---------------------------------------------------------------------------
# Test: invalid signature returns 401
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_slash_command_invalid_signature_returns_401(client_with_secret: AsyncClient) -> None:
    """POST /webhooks/slack/commands with invalid signature returns 401."""
    form_body = b"command=%2Fbernstein&text=fix+bug&user_id=U1&channel_id=C1"
    resp = await client_with_secret.post(
        "/webhooks/slack/commands",
        content=form_body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": str(int(time.time())),
            "x-slack-signature": "v0=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        },
    )
    assert resp.status_code == 401
    assert "Invalid Slack signature" in resp.json()["detail"]


@pytest.mark.anyio
async def test_slash_command_missing_signature_headers_returns_401(client_with_secret: AsyncClient) -> None:
    """POST /webhooks/slack/commands without signature headers returns 401."""
    form_body = b"command=%2Fbernstein&text=fix+bug&user_id=U1&channel_id=C1"
    resp = await client_with_secret.post(
        "/webhooks/slack/commands",
        content=form_body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_slash_command_valid_signature_accepted(client_with_secret: AsyncClient) -> None:
    """POST /webhooks/slack/commands with valid signature returns 200."""
    form_body = b"command=%2Fbernstein&text=deploy+staging&user_id=U1&channel_id=C1"
    sig_headers = _slack_sig_headers(form_body, "test_secret_key")
    resp = await client_with_secret.post(
        "/webhooks/slack/commands",
        content=form_body,
        headers={"content-type": "application/x-www-form-urlencoded", **sig_headers},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: empty command text returns error message
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_slash_command_empty_text_returns_error(client: AsyncClient) -> None:
    """POST /webhooks/slack/commands with empty text returns an error message."""
    form_body = b"command=%2Fbernstein&text=&user_id=U123&channel_id=C456"
    resp = await client.post(
        "/webhooks/slack/commands",
        content=form_body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "no task text provided" in data["text"]
    # No task should have been created
    tasks_resp = await client.get("/tasks?status=open")
    assert tasks_resp.status_code == 200
    assert tasks_resp.json() == []
