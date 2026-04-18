"""Tests for POST /webhook — generic inbound webhook task creation."""

from __future__ import annotations

import json
import time
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


def _signed(
    body: bytes,
    secret: str = _WEBHOOK_SECRET,
    *,
    ts: int | None = None,
    signed_ts: int | None = None,
) -> dict[str, str]:
    """Build valid HMAC + timestamp headers for ``POST /webhook`` (audit-121).

    ``ts`` is the value placed in the ``X-Bernstein-Timestamp`` header.
    ``signed_ts`` is the value used when deriving the HMAC input —
    defaults to ``ts`` so the signature matches.  Pass a different
    ``signed_ts`` to construct a mismatched request for negative tests.
    """

    timestamp = int(time.time()) if ts is None else ts
    signature_ts = timestamp if signed_ts is None else signed_ts
    signed_payload = f"{signature_ts}.".encode() + body
    return {
        "content-type": "application/json",
        "x-bernstein-timestamp": str(timestamp),
        "x-bernstein-webhook-signature-256": sign_hmac_sha256(secret, signed_payload, prefix="sha256="),
    }


# ---------------------------------------------------------------------------
# Basic creation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_creates_task(client: AsyncClient) -> None:
    """POST /webhook returns 201 and a task nested in .task."""
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    resp = await client.post("/webhook", content=body, headers=_signed(body))
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
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    resp = await client.post("/webhook", content=body, headers=_signed(body))
    assert resp.status_code == 201
    assert resp.json()["task"]["role"] == "backend"


@pytest.mark.anyio
async def test_webhook_accepts_explicit_role(client: AsyncClient) -> None:
    """POST /webhook respects an explicit role in the payload."""
    payload = {**_WEBHOOK_PAYLOAD, "role": "qa"}
    body = json.dumps(payload).encode()
    resp = await client.post("/webhook", content=body, headers=_signed(body))
    assert resp.status_code == 201
    assert resp.json()["task"]["role"] == "qa"


@pytest.mark.anyio
async def test_webhook_task_is_retrievable(client: AsyncClient) -> None:
    """Task created via /webhook can be fetched from GET /tasks/{id}."""
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    resp = await client.post("/webhook", content=body, headers=_signed(body))
    task_id = resp.json()["task"]["id"]

    get_resp = await client.get(f"/tasks/{task_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == task_id


# ---------------------------------------------------------------------------
# audit-042: endpoint disabled when secret unset
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_no_secret_configured_disables_endpoint(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BERNSTEIN_WEBHOOK_SECRET is unset, the endpoint is disabled (audit-042)."""
    monkeypatch.delenv("BERNSTEIN_WEBHOOK_SECRET", raising=False)
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD)
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# audit-121: plaintext fallback removed; timestamp freshness enforced
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_plaintext_secret_header_rejected(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-121: the plaintext ``X-Bernstein-Webhook-Secret`` header is no longer honoured."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    resp = await client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-bernstein-webhook-secret": _WEBHOOK_SECRET,
        },
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_missing_timestamp_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-121: missing ``X-Bernstein-Timestamp`` is rejected."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    # Sign the raw body (legacy shape) but drop the timestamp header.
    signature = sign_hmac_sha256(_WEBHOOK_SECRET, body, prefix="sha256=")
    resp = await client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-bernstein-webhook-signature-256": signature,
        },
    )
    assert resp.status_code == 401
    assert "timestamp" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_webhook_malformed_timestamp_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-121: non-numeric timestamps are treated as missing."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    # Start from a valid header set, then clobber the timestamp.
    headers = dict(_signed(body))
    headers["x-bernstein-timestamp"] = "not-a-number"
    resp = await client.post("/webhook", content=body, headers=headers)
    assert resp.status_code == 401
    assert "timestamp" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_webhook_stale_timestamp_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-121: timestamps older than five minutes are rejected even with valid HMAC."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    stale_ts = int(time.time()) - 10 * 60  # ten minutes in the past
    resp = await client.post("/webhook", content=body, headers=_signed(body, ts=stale_ts))
    assert resp.status_code == 401
    assert "timestamp" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_webhook_future_timestamp_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-121: timestamps more than five minutes in the future are rejected."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    future_ts = int(time.time()) + 10 * 60
    resp = await client.post("/webhook", content=body, headers=_signed(body, ts=future_ts))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_timestamp_header_bound_into_hmac(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-121: rewriting the timestamp header after signing invalidates the HMAC."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    now = int(time.time())
    # Sign with one timestamp, advertise another — the server must reject.
    headers = _signed(body, ts=now, signed_ts=now - 1)
    resp = await client.post("/webhook", content=body, headers=headers)
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_without_signature_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST without HMAC signature must return 401 even with a fresh timestamp."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    resp = await client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-bernstein-timestamp": str(int(time.time())),
        },
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_with_wrong_signature_returns_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST with a wrong HMAC signature must return 401."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    headers = dict(_signed(body))
    headers["x-bernstein-webhook-signature-256"] = "sha256=deadbeef"
    resp = await client.post("/webhook", content=body, headers=headers)
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_with_correct_signature_creates_task(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST with a valid HMAC + timestamp creates a task."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    resp = await client.post("/webhook", content=body, headers=_signed(body))
    assert resp.status_code == 201
    assert resp.json()["task"]["title"] == _WEBHOOK_PAYLOAD["title"]


@pytest.mark.anyio
async def test_webhook_endpoint_disabled_without_secret(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoint returns 503 when no secret is configured, even for signed requests."""
    monkeypatch.delenv("BERNSTEIN_WEBHOOK_SECRET", raising=False)
    body = json.dumps(_WEBHOOK_PAYLOAD).encode()
    resp = await client.post(
        "/webhook",
        content=body,
        headers=_signed(body, secret="attacker-guess"),
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
async def test_github_webhook_rejects_stale_timestamp_when_supplied(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """audit-121: github endpoint enforces timestamp freshness when the header is present."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "gh-secret")
    body = b'{"action":"opened","repository":{"full_name":"o/r"}}'
    stale_ts = int(time.time()) - 10 * 60
    resp = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "Content-Type": "application/json",
            "X-Bernstein-Timestamp": str(stale_ts),
            "X-Hub-Signature-256": sign_hmac_sha256("gh-secret", body, prefix="sha256="),
        },
    )
    assert resp.status_code == 401


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


@pytest.mark.anyio
async def test_gitlab_webhook_rejects_stale_timestamp_when_supplied(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """audit-121: gitlab endpoint enforces timestamp freshness when the header is present."""
    monkeypatch.setenv("GITLAB_WEBHOOK_TOKEN", "gitlab-top-secret")
    stale_ts = int(time.time()) - 10 * 60
    resp = await client.post(
        "/webhooks/gitlab",
        content=b'{"object_kind":"pipeline","object_attributes":{"status":"success"}}',
        headers={
            "Content-Type": "application/json",
            "X-Gitlab-Token": "gitlab-top-secret",
            "X-Bernstein-Timestamp": str(stale_ts),
        },
    )
    assert resp.status_code == 401
