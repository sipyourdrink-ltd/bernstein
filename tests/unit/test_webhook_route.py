"""Tests for the generic webhook task-creation route."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from bernstein.core.webhook_signatures import sign_hmac_sha256
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


_WEBHOOK_SECRET = "top-secret"


@pytest.fixture()
def app(jsonl_path: Path, monkeypatch: pytest.MonkeyPatch):
    # audit-042: /webhook requires BERNSTEIN_WEBHOOK_SECRET to be
    # configured - the endpoint 503s without it.  Set a stable secret
    # so the "happy path" tests in this file can exercise the route.
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
def app_with_auth(jsonl_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    return create_app(jsonl_path=jsonl_path, auth_token="server-token")


@pytest.fixture()
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
async def auth_client(app_with_auth) -> AsyncClient:
    transport = ASGITransport(app=app_with_auth)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _signed_headers(
    body: bytes,
    secret: str = _WEBHOOK_SECRET,
    *,
    ts: int | None = None,
) -> dict[str, str]:
    """Build HMAC + timestamp headers for a generic-webhook request (audit-121)."""

    timestamp = int(time.time()) if ts is None else ts
    signed = f"{timestamp}.".encode() + body
    return {
        "content-type": "application/json",
        "x-bernstein-timestamp": str(timestamp),
        "x-bernstein-webhook-signature-256": sign_hmac_sha256(secret, signed, prefix="sha256="),
    }


@pytest.mark.anyio
async def test_generic_webhook_creates_task_with_defaults(client: AsyncClient) -> None:
    body = json.dumps({"title": "Fix flaky login", "description": "Investigate the login test flake."}).encode()
    response = await client.post("/webhook", content=body, headers=_signed_headers(body))

    assert response.status_code == 201
    task = response.json()["task"]
    assert task["title"] == "Fix flaky login"
    assert task["role"] == "backend"
    assert task["priority"] == 2
    assert task["scope"] == "medium"
    assert task["complexity"] == "medium"


@pytest.mark.anyio
async def test_generic_webhook_is_public_when_server_auth_is_enabled(
    auth_client: AsyncClient,
) -> None:
    body = json.dumps({"title": "Create task", "description": "This should bypass bearer auth."}).encode()
    response = await auth_client.post("/webhook", content=body, headers=_signed_headers(body))

    assert response.status_code == 201
    assert response.json()["task"]["title"] == "Create task"


@pytest.mark.anyio
async def test_generic_webhook_enforces_hmac_only(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-121: plaintext fallback is gone - HMAC + timestamp required."""

    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps({"title": "Allowed", "description": "Correct HMAC signature."}).encode()

    missing = await client.post("/webhook", content=body, headers={"content-type": "application/json"})
    assert missing.status_code == 401

    plaintext = await client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-bernstein-webhook-secret": _WEBHOOK_SECRET,
        },
    )
    assert plaintext.status_code == 401

    wrong_sig = _signed_headers(body).copy()
    wrong_sig["x-bernstein-webhook-signature-256"] = "sha256=deadbeef"
    rejected = await client.post("/webhook", content=body, headers=wrong_sig)
    assert rejected.status_code == 401

    allowed = await client.post("/webhook", content=body, headers=_signed_headers(body))
    assert allowed.status_code == 201
    assert allowed.json()["task"]["title"] == "Allowed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("cooldown", "second_title", "reason"), [(300, "Build second", "cooldown"), (0, "Build first", "deduplicated")]
)
async def test_generic_webhook_applies_matching_trigger_rules(
    app, client: AsyncClient, tmp_path: Path, cooldown: int, second_title: str, reason: str
) -> None:
    from bernstein.core.orchestration.trigger_manager import TriggerManager

    sdd_dir = tmp_path / ".sdd"
    config = sdd_dir / "config" / "triggers.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
triggers:
  - name: inbound
    source: webhook
    filters:
      path: /web*
    conditions:
      cooldown_s: {cooldown}
""",
        encoding="utf-8",
    )
    app.state.sdd_dir = sdd_dir

    async def post(title: str, trigger_id: str):
        body = json.dumps({"title": title, "description": "Synthetic build event"}).encode()
        return await client.post(
            "/webhook", content=body, headers={**_signed_headers(body), "x-bernstein-trigger-id": trigger_id}
        )

    first = await post("Build first", "delivery-1")
    assert first.status_code == 201, first.text
    replay = await post("Build first", "delivery-1")
    assert replay.status_code == 409, replay.text
    assert replay.json()["receipt"]["refusal_reason"] == "replayed_trigger_id"

    second = await post(second_title, "delivery-2")
    assert second.status_code == 409, second.text
    assert reason in second.json()["detail"]
    assert second.json()["receipt"]["outcome"] == "refused"
    assert second.json()["receipt"]["refusal_reason"] == "trigger_policy_suppressed"
    config.write_text(config.read_text().replace("/web*", "/another*"), encoding="utf-8")
    other = await post("Unmatched event", "delivery-2")
    assert other.status_code == 201, other.text

    history = TriggerManager(sdd_dir).get_fire_history()
    assert [(row["task_id"], row["produced"]) for row in history] == [
        (first.json()["task"]["id"], True),
        ("", False),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(("with_nonce", "history"), [(False, b"\xff"), (False, b"[]\n"), (True, b"\xff")])
async def test_generic_webhook_reports_unconfirmed_reservation(
    app, client: AsyncClient, tmp_path: Path, with_nonce: bool, history: bytes
) -> None:
    from unittest.mock import AsyncMock, patch

    sdd_dir = tmp_path / ".sdd"
    config = sdd_dir / "config" / "triggers.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "triggers:\n  - name: inbound\n    source: webhook\n    conditions:\n      cooldown_s: 300\n", encoding="utf-8"
    )
    app.state.sdd_dir = sdd_dir
    body = b'{"title":"Build","description":"Synthetic build event"}'

    headers = _signed_headers(body)
    if with_nonce:
        headers["x-bernstein-trigger-id"] = "delivery-with-explicit-nonce"

    with patch.object(app.state.store, "create", new=AsyncMock(side_effect=RuntimeError("store unavailable"))):
        failed = await client.post("/webhook", content=body, headers=headers)
        assert failed.status_code == 500

    (sdd_dir / "runtime" / "triggers" / "fire_log.jsonl").write_bytes(history)
    response = await client.post("/webhook", content=body, headers=headers)
    if with_nonce:
        assert response.status_code == 409, response.text
        assert response.json()["receipt"]["refusal_reason"] == "replayed_trigger_id"
    else:
        assert response.status_code == 503, response.text
        assert response.json()["receipt"]["refusal_reason"] == "trigger_delivery_unconfirmed"

    (sdd_dir / "runtime" / "triggers" / "fire_log.jsonl").write_text(
        '{"trigger_name":"inbound","cooldown_scope":"default","fired_at":"invalid"}\n', encoding="utf-8"
    )
    another_body = b'{"title":"Another build","description":"Synthetic build event"}'
    fire_log = sdd_dir / "runtime" / "triggers" / "fire_log.jsonl"
    for malformed in (fire_log.read_bytes(), b"{broken"):
        fire_log.write_bytes(malformed)
        unavailable = await client.post("/webhook", content=another_body, headers=_signed_headers(another_body))
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json()["receipt"]["refusal_reason"] == "trigger_policy_unavailable"


@pytest.mark.anyio
async def test_generic_webhook_requires_authenticated_headers_for_header_filters(
    app, client: AsyncClient, tmp_path: Path
) -> None:
    sdd_dir = tmp_path / ".sdd"
    config = sdd_dir / "config" / "triggers.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "triggers:\n  - name: inbound\n    source: webhook\n    filters:\n      headers:\n        x-event-kind: build\n",
        encoding="utf-8",
    )
    app.state.sdd_dir = sdd_dir
    body = b'{"title":"Build","description":"Synthetic build event"}'
    response = await client.post("/webhook", content=body, headers={**_signed_headers(body), "x-event-kind": "build"})
    assert response.status_code == 409, response.text
    assert "authenticated headers" in response.json()["detail"]
    assert response.json()["receipt"]["outcome"] == "refused"
