"""Tests for POST /webhook — generic inbound webhook task creation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.webhook_signatures import sign_hmac_sha256
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


_WEBHOOK_SECRET = "s3cr3t"


@pytest.fixture()
def app(jsonl_path: Path, monkeypatch: pytest.MonkeyPatch):
    # audit-042: endpoint now fail-closes when the secret is unset —
    # every basic-creation test must therefore configure the secret and
    # pass it on each request.
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


_WEBHOOK_PAYLOAD = {
    "title": "Fix login bug",
    "description": "Users cannot log in with SSO",
}
_SECRET_HEADERS = {"x-bernstein-webhook-secret": _WEBHOOK_SECRET}


# ---------------------------------------------------------------------------
# Basic creation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_creates_task(client: AsyncClient) -> None:
    """POST /webhook returns 201 and a task nested in .task."""
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD, headers=_SECRET_HEADERS)
    assert resp.status_code == 201
    data = resp.json()
    assert "task" in data
    task = data["task"]
    assert task["title"] == "Fix login bug"
    assert task["status"] == "open"
    assert task["id"]


@pytest.mark.anyio
async def test_webhook_defaults_role_to_backend(client: AsyncClient) -> None:
    """POST /webhook uses 'backend' as the default role."""
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD, headers=_SECRET_HEADERS)
    assert resp.status_code == 201
    assert resp.json()["task"]["role"] == "backend"


@pytest.mark.anyio
async def test_webhook_accepts_explicit_role(client: AsyncClient) -> None:
    """POST /webhook respects an explicit role in the payload."""
    resp = await client.post(
        "/webhook",
        json={**_WEBHOOK_PAYLOAD, "role": "qa"},
        headers=_SECRET_HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["task"]["role"] == "qa"


@pytest.mark.anyio
async def test_webhook_task_is_retrievable(client: AsyncClient) -> None:
    """Task created via /webhook can be fetched from GET /tasks/{id}."""
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD, headers=_SECRET_HEADERS)
    task_id = resp.json()["task"]["id"]

    get_resp = await client.get(f"/tasks/{task_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == task_id


# ---------------------------------------------------------------------------
# Secret verification
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_no_secret_configured_disables_endpoint(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BERNSTEIN_WEBHOOK_SECRET is unset, the endpoint is disabled (audit-042).

    Fail-closed: unsigned POSTs must never create tasks.  The server
    returns 503 to signal that the operator has not enabled the
    endpoint rather than 401 — the problem is a server configuration
    gap, not a bad caller.
    """
    monkeypatch.delenv("BERNSTEIN_WEBHOOK_SECRET", raising=False)
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD)
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_webhook_correct_secret_accepted(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Correct X-Bernstein-Webhook-Secret header is accepted when secret is configured."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", "s3cr3t")
    resp = await client.post(
        "/webhook",
        json=_WEBHOOK_PAYLOAD,
        headers={"X-Bernstein-Webhook-Secret": "s3cr3t"},
    )
    assert resp.status_code == 201


@pytest.mark.anyio
async def test_webhook_wrong_secret_rejected(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrong secret returns 401."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", "s3cr3t")
    resp = await client.post(
        "/webhook",
        json=_WEBHOOK_PAYLOAD,
        headers={"X-Bernstein-Webhook-Secret": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_missing_secret_header_rejected(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing secret header returns 401 when secret is configured."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", "s3cr3t")
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# audit-042: fail-closed behaviour on HMAC signature path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_without_signature_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-042 (a): POST without HMAC signature must return 401.

    When the secret is configured, an unsigned POST — even with a
    well-formed JSON body — must never create a task.
    """
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    resp = await client.post(
        "/webhook",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_with_wrong_signature_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-042 (b): POST with a wrong HMAC signature must return 401."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    resp = await client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-bernstein-webhook-signature-256": "sha256=deadbeef",
        },
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_with_correct_signature_creates_task(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """audit-042 (c): POST with a valid HMAC signature creates a task."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    signature = sign_hmac_sha256(_WEBHOOK_SECRET, body, prefix="sha256=")
    resp = await client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-bernstein-webhook-signature-256": signature,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["task"]["title"] == _WEBHOOK_PAYLOAD["title"]


@pytest.mark.anyio
async def test_webhook_endpoint_disabled_without_secret(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-042 (d): endpoint returns 503 when no secret is configured.

    A signed request should also be rejected — the endpoint is
    *disabled*, not just missing auth — because the caller cannot prove
    intent without a shared secret on the server side.
    """
    monkeypatch.delenv("BERNSTEIN_WEBHOOK_SECRET", raising=False)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    # Even a valid-looking signature must be rejected — server has no secret.
    signature = sign_hmac_sha256("attacker-guess", body, prefix="sha256=")
    resp = await client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-bernstein-webhook-signature-256": signature,
        },
    )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# audit-042: GitHub + GitLab endpoints are disabled when the secret is unset
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_github_webhook_endpoint_disabled_without_secret(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/webhooks/github returns 503 when GITHUB_WEBHOOK_SECRET is unset."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    body = b'{"action":"opened"}'
    resp = await client.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "issues", "Content-Type": "application/json"},
    )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_gitlab_webhook_endpoint_disabled_without_token(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/webhooks/gitlab returns 503 when GITLAB_WEBHOOK_TOKEN is unset."""
    monkeypatch.delenv("GITLAB_WEBHOOK_TOKEN", raising=False)
    resp = await client.post(
        "/webhooks/gitlab",
        content=b'{"object_kind":"pipeline"}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_gitlab_webhook_missing_token_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """/webhooks/gitlab returns 401 when token is set server-side but not sent."""
    monkeypatch.setenv("GITLAB_WEBHOOK_TOKEN", "gitlab-top-secret")
    resp = await client.post(
        "/webhooks/gitlab",
        content=b'{"object_kind":"pipeline"}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert "missing" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_gitlab_webhook_wrong_token_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """/webhooks/gitlab returns 401 when the provided token does not match."""
    monkeypatch.setenv("GITLAB_WEBHOOK_TOKEN", "gitlab-top-secret")
    resp = await client.post(
        "/webhooks/gitlab",
        content=b'{"object_kind":"pipeline"}',
        headers={
            "Content-Type": "application/json",
            "X-Gitlab-Token": "wrong",
        },
    )
    assert resp.status_code == 401
    assert "invalid" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_gitlab_webhook_correct_token_accepted(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """/webhooks/gitlab returns 200 when the token matches (no actionable payload)."""
    monkeypatch.setenv("GITLAB_WEBHOOK_TOKEN", "gitlab-top-secret")
    # Non-failure pipeline → no task created, but request is accepted.
    resp = await client.post(
        "/webhooks/gitlab",
        content=b'{"object_kind":"pipeline","object_attributes":{"status":"success"}}',
        headers={
            "Content-Type": "application/json",
            "X-Gitlab-Token": "gitlab-top-secret",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["tasks_created"] == 0
