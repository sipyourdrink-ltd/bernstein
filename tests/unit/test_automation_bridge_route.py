"""Automation bridge round trip over POST /webhook (#2512).

Each documented recipe (n8n, Zapier, Workato) is exercised end to end against a
local server: the trigger is admitted, the receipt comes back in the response,
it verifies offline against the chain, and a replay of the same execution id is
refused with its own signed refusal receipt.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.server import create_app
from bernstein.core.trigger_sources.receipt import (
    REFUSAL_REPLAYED_TRIGGER,
    REFUSAL_UNAUTHENTICATED,
    TRIGGER_OUTCOME_ADMITTED,
    TRIGGER_OUTCOME_REFUSED,
    verify_receipt_document,
)

_WEBHOOK_SECRET = "s3cr3t"

#: Header, envelope shape, and platform label for each documented recipe.
_RECIPES = {
    "n8n": ("x-n8n-execution-id", lambda p: {"body": p}),
    "zapier": ("x-zapier-request-id", lambda p: {"data": p}),
    "workato": ("x-workato-job-id", lambda p: {"input": p}),
}


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    """Return an isolated ``.sdd`` layout the app will resolve state under."""
    return tmp_path / ".sdd"


@pytest.fixture()
def app(sdd_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.delenv("BERNSTEIN_AUTOMATION_BRIDGE_ROOT", raising=False)
    jsonl_path = sdd_dir / "tasks" / "tasks.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _signed(body: bytes, extra: dict[str, str] | None = None) -> dict[str, str]:
    from bernstein.core.webhook_signatures import sign_hmac_sha256

    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode() + body
    headers = {
        "content-type": "application/json",
        "x-bernstein-timestamp": str(timestamp),
        "x-bernstein-webhook-signature-256": sign_hmac_sha256(_WEBHOOK_SECRET, signed_payload, prefix="sha256="),
    }
    return headers | (extra or {})


def _payload(platform: str, *, title: str = "Rotate the deploy key") -> bytes:
    """Return a body shaped the way ``platform`` posts it.

    The task fields stay at the top level so the existing ``POST /webhook``
    contract is satisfied; the platform envelope is additive alongside them,
    which is exactly what the documented recipes send.
    """
    task = {"title": title, "description": "quarterly rotation"}
    _, wrap = _RECIPES[platform]
    return json.dumps(task | wrap(task)).encode()


def _verify(sdd_dir: Path, tmp_path: Path, document: dict[str, Any], **kw: Any):
    return verify_receipt_document(
        document,
        audit_dir=sdd_dir / "audit",
        hmac_key=load_or_create_audit_key(tmp_path / "audit.key"),
        **kw,
    )


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("platform", sorted(_RECIPES))
async def test_recipe_round_trips_and_the_receipt_verifies(
    client: AsyncClient,
    sdd_dir: Path,
    tmp_path: Path,
    platform: str,
) -> None:
    """Each documented recipe is admitted and returns a verifiable receipt."""
    header, _ = _RECIPES[platform]
    body = _payload(platform)
    resp = await client.post("/webhook", content=body, headers=_signed(body, {header: f"{platform}-exec-1"}))

    assert resp.status_code == 201
    payload = resp.json()
    assert payload["task"]["title"] == "Rotate the deploy key"

    receipt = payload["receipt"]
    assert receipt is not None
    assert receipt["platform"] == platform
    assert receipt["outcome"] == TRIGGER_OUTCOME_ADMITTED
    assert receipt["trigger_id"] == f"{platform}-exec-1"
    assert receipt["replay_protected"] is True

    result = _verify(sdd_dir, tmp_path, receipt, body=body)
    assert result.ok, result.reason


@pytest.mark.anyio
async def test_receipt_binds_the_payload_that_was_admitted(
    client: AsyncClient,
    sdd_dir: Path,
    tmp_path: Path,
) -> None:
    """Verifying the receipt against a different payload fails."""
    body = _payload("n8n")
    resp = await client.post("/webhook", content=body, headers=_signed(body, {"x-n8n-execution-id": "e-1"}))
    receipt = resp.json()["receipt"]

    result = _verify(sdd_dir, tmp_path, receipt, body=body.replace(b"quarterly", b"emergency"))
    assert not result.ok
    assert "payload digest" in result.reason


@pytest.mark.anyio
async def test_tampering_with_the_stored_receipt_fails(
    client: AsyncClient,
    sdd_dir: Path,
    tmp_path: Path,
) -> None:
    """A platform-stored receipt edited after the fact does not verify."""
    body = _payload("workato")
    resp = await client.post("/webhook", content=body, headers=_signed(body, {"x-workato-job-id": "j-1"}))
    receipt = dict(resp.json()["receipt"])
    receipt["scope"] = "task:create admin:all"

    result = _verify(sdd_dir, tmp_path, receipt)
    assert not result.ok


# ---------------------------------------------------------------------------
# Refusal paths
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_replayed_execution_id_is_refused_with_a_signed_receipt(
    client: AsyncClient,
    sdd_dir: Path,
    tmp_path: Path,
) -> None:
    """A re-sent execution id is refused and the refusal is itself verifiable."""
    body = _payload("zapier")
    headers = {"x-zapier-request-id": "zap-replay"}

    first = await client.post("/webhook", content=body, headers=_signed(body, headers))
    assert first.status_code == 201

    second = await client.post("/webhook", content=body, headers=_signed(body, headers))
    assert second.status_code == 409
    receipt = second.json()["receipt"]
    assert receipt["outcome"] == TRIGGER_OUTCOME_REFUSED
    assert receipt["refusal_reason"] == REFUSAL_REPLAYED_TRIGGER

    result = _verify(sdd_dir, tmp_path, receipt)
    assert result.ok, result.reason


@pytest.mark.anyio
async def test_replay_does_not_create_a_second_task(client: AsyncClient) -> None:
    """The refused replay leaves the task store untouched."""
    body = _payload("n8n")
    headers = {"x-n8n-execution-id": "only-once"}

    await client.post("/webhook", content=body, headers=_signed(body, headers))
    await client.post("/webhook", content=body, headers=_signed(body, headers))

    listing = await client.get("/tasks")
    titles = [t["title"] for t in listing.json()]
    assert titles.count("Rotate the deploy key") == 1


@pytest.mark.anyio
async def test_forged_signature_is_refused_with_a_signed_refusal(
    client: AsyncClient,
    sdd_dir: Path,
    tmp_path: Path,
) -> None:
    """A bad signature is refused and the refusal is recorded, not dropped."""
    body = _payload("n8n")
    headers = _signed(body, {"x-n8n-execution-id": "forged"})
    headers["x-bernstein-webhook-signature-256"] = "sha256=" + "0" * 64

    resp = await client.post("/webhook", content=body, headers=headers)
    assert resp.status_code == 401
    receipt = resp.json()["receipt"]
    assert receipt["outcome"] == TRIGGER_OUTCOME_REFUSED
    assert receipt["refusal_reason"] == REFUSAL_UNAUTHENTICATED
    # A refused trigger is granted nothing and projects nothing.
    assert receipt["scope"] == ""
    assert receipt["graph_digest"] == ""

    result = _verify(sdd_dir, tmp_path, receipt, body=body)
    assert result.ok, result.reason


@pytest.mark.anyio
async def test_forged_trigger_creates_no_task(client: AsyncClient) -> None:
    """The refused trigger never reaches the store."""
    body = _payload("n8n")
    headers = _signed(body, {"x-n8n-execution-id": "forged-2"})
    headers["x-bernstein-webhook-signature-256"] = "sha256=" + "0" * 64
    await client.post("/webhook", content=body, headers=headers)

    listing = await client.get("/tasks")
    assert listing.json() == []


@pytest.mark.anyio
async def test_unconfigured_endpoint_mints_no_receipt(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing secret is a deployment fault, not a refused trigger."""
    monkeypatch.delenv("BERNSTEIN_WEBHOOK_SECRET", raising=False)
    body = _payload("n8n")
    resp = await client.post("/webhook", content=body, headers=_signed(body))

    assert resp.status_code == 503
    assert "receipt" not in resp.json()


# ---------------------------------------------------------------------------
# Determinism across operators
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_identical_payloads_project_the_same_graph_digest(client: AsyncClient) -> None:
    """Two triggers carrying the same payload prove they fired the same graph."""
    body = _payload("n8n")

    first = await client.post("/webhook", content=body, headers=_signed(body, {"x-n8n-execution-id": "a"}))
    second = await client.post("/webhook", content=body, headers=_signed(body, {"x-n8n-execution-id": "b"}))

    left = first.json()["receipt"]
    right = second.json()["receipt"]
    assert left["payload_digest"] == right["payload_digest"]
    assert left["graph_digest"] == right["graph_digest"]
    # Distinct admissions all the same: different nonce, different anchor.
    assert left["chain_entry_hash"] != right["chain_entry_hash"]


@pytest.mark.anyio
async def test_caller_without_a_nonce_is_admitted_but_says_so(client: AsyncClient) -> None:
    """No execution id means no replay guarantee, and the receipt records that."""
    body = _payload("n8n")
    resp = await client.post("/webhook", content=body, headers=_signed(body))

    assert resp.status_code == 201
    receipt = resp.json()["receipt"]
    assert receipt["replay_protected"] is False
    assert receipt["platform"] == "generic"


@pytest.mark.anyio
async def test_repeated_request_without_a_nonce_is_not_refused(client: AsyncClient) -> None:
    """A derived id never turns a legitimate re-fire into a refusal."""
    body = _payload("n8n")
    first = await client.post("/webhook", content=body, headers=_signed(body))
    second = await client.post("/webhook", content=body, headers=_signed(body))

    assert first.status_code == 201
    assert second.status_code == 201
