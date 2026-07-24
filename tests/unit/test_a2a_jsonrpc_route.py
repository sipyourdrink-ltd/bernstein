"""End-to-end A2A JSON-RPC server surface over the real ASGI app (#2609).

The binding design directive's acceptance addition, exercised here with an
independent JSON-RPC speaker (not the server's own client code):

    an off-the-shelf A2A client discovers the card at both well-known paths,
    sends ``message/send``, polls ``tasks/get`` to completion, and receives an
    artifact whose lineage receipt verifies offline.

Plus the surrounding guarantees: the surface is off by default, both auth
schemes are enforced and rejected per spec, and the card advertises the
JSON-RPC binding and both schemes only when enabled.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.interop.a2a_card import SignedCapabilityCard, verify_capability_card
from bernstein.core.protocols.a2a.receipt import A2ATaskReceipt, verify_task_receipt
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def enabled_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Enable the surface and configure both auth schemes, hermetically."""
    monkeypatch.setenv("BERNSTEIN_A2A_SERVER_ENABLED", "1")
    monkeypatch.setenv("BERNSTEIN_A2A_API_KEYS", "alice=key-alice")
    monkeypatch.setenv("BERNSTEIN_A2A_OAUTH_CLIENTS", "client-x=secret-x")
    monkeypatch.setenv("BERNSTEIN_A2A_OAUTH_SIGNING_SECRET", "unit-signing-secret")
    # Isolate the agent-card keystore so the test never touches a real one.
    monkeypatch.setenv("BERNSTEIN_AGENT_CARD_KEY_DIR", str(tmp_path / "keys"))
    from bernstein.core.routes import well_known

    well_known._reset_signing_keypair_for_tests(tmp_path / "keys")


@pytest.fixture()
def app(tmp_path: Path, enabled_env: None):  # type: ignore[no-untyped-def]
    # ``enabled_env`` must run first so ``create_app`` reads the A2A auth
    # config (built once at app creation) with the env already in place.
    return create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Minimal, independent A2A JSON-RPC client (not the server's own code)
# ---------------------------------------------------------------------------


class _A2AClient:
    """A tiny off-the-shelf-style JSON-RPC 2.0 A2A speaker."""

    def __init__(self, http: AsyncClient, *, api_key: str | None = None, bearer: str | None = None) -> None:
        self._http = http
        self._id = 0
        self._headers: dict[str, str] = {}
        if api_key is not None:
            self._headers["X-API-Key"] = api_key
        if bearer is not None:
            self._headers["Authorization"] = f"Bearer {bearer}"

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self._id += 1
        return await self._http.post(
            "/a2a/v1",
            headers=self._headers,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
        )

    @staticmethod
    def text_message(text: str) -> dict[str, Any]:
        return {"message": {"role": "user", "parts": [{"kind": "text", "text": text}], "messageId": "m-1"}}


async def _drive_to_completion(app, a2a_task_id: str, result: str) -> None:
    """Act as the worker: complete the Bernstein task behind the A2A task.

    Walks the legal lifecycle (open -> claimed -> done) the store enforces,
    standing in for the agent that would normally run the task.
    """
    handler = app.state.a2a_handler
    bernstein_task_id = handler.get_task(a2a_task_id).bernstein_task_id
    await app.state.store.claim_by_id(bernstein_task_id)
    await app.state.store.complete(bernstein_task_id, result)


# ---------------------------------------------------------------------------
# Off-by-default
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_surface_is_off_by_default(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_A2A_SERVER_ENABLED", raising=False)
    resp = await client.post(
        "/a2a/v1",
        headers={"X-API-Key": "anything"},
        json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_card_does_not_advertise_the_surface_when_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("BERNSTEIN_A2A_SERVER_ENABLED", raising=False)
    monkeypatch.setenv("BERNSTEIN_AGENT_CARD_KEY_DIR", str(tmp_path / "k"))
    from bernstein.core.routes import well_known

    well_known._reset_signing_keypair_for_tests(tmp_path / "k")
    card = (await client.get("/.well-known/agent-card.json")).json()
    assert "JSONRPC" not in card["supportedInterfaces"]
    assert "additionalInterfaces" not in card


# ---------------------------------------------------------------------------
# Discovery: card at both paths, advertising the binding + schemes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_card_served_identically_at_both_well_known_paths(client: AsyncClient, enabled_env: None) -> None:
    legacy = await client.get("/.well-known/agent.json")
    canonical = await client.get("/.well-known/agent-card.json")
    assert legacy.status_code == canonical.status_code == 200
    assert legacy.content == canonical.content, "both paths must serve byte-identical signed bytes"


@pytest.mark.anyio
async def test_discovered_card_verifies_offline(client: AsyncClient, enabled_env: None) -> None:
    card = (await client.get("/.well-known/agent-card.json")).json()
    signed = SignedCapabilityCard.from_dict(card["capabilityCard"])
    assert verify_capability_card(signed) is True


@pytest.mark.anyio
async def test_tampered_card_fails_offline_verification(client: AsyncClient, enabled_env: None) -> None:
    card = (await client.get("/.well-known/agent-card.json")).json()
    signed = SignedCapabilityCard.from_dict(card["capabilityCard"])
    tampered = SignedCapabilityCard.from_dict(card["capabilityCard"])
    object.__setattr__(tampered.card, "name", signed.card.name + "-evil")
    assert verify_capability_card(tampered) is False


@pytest.mark.anyio
async def test_card_advertises_jsonrpc_binding_and_both_schemes(client: AsyncClient, enabled_env: None) -> None:
    card = (await client.get("/.well-known/agent-card.json")).json()
    assert "JSONRPC" in card["supportedInterfaces"]
    jsonrpc = [i for i in card["additionalInterfaces"] if i["transport"] == "JSONRPC"]
    assert jsonrpc and jsonrpc[0]["url"].endswith("/a2a/v1")
    scheme_types = {s["type"] for s in card["securitySchemes"]}
    assert "apiKey" in scheme_types
    assert "oauth2" in scheme_types


# ---------------------------------------------------------------------------
# Auth: rejections per spec
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unauthenticated_call_is_rejected_with_a_challenge(client: AsyncClient, enabled_env: None) -> None:
    resp = await client.post("/a2a/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}})
    assert resp.status_code == 401
    assert "www-authenticate" in {k.lower() for k in resp.headers}


@pytest.mark.anyio
async def test_bad_api_key_is_rejected(client: AsyncClient, enabled_env: None) -> None:
    c = _A2AClient(client, api_key="wrong")
    resp = await c.call("tasks/get", {"id": "x"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# OAuth2 client-credentials token flow
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_oauth_token_endpoint_issues_a_usable_bearer(client: AsyncClient, enabled_env: None) -> None:
    token_resp = await client.post(
        "/a2a/v1/oauth/token",
        data={"grant_type": "client_credentials", "client_id": "client-x", "client_secret": "secret-x"},
    )
    assert token_resp.status_code == 200
    token = token_resp.json()["access_token"]

    c = _A2AClient(client, bearer=token)
    send = await c.call("message/send", _A2AClient.text_message("do the thing"))
    assert send.status_code == 200
    assert send.json()["result"]["kind"] == "task"


@pytest.mark.anyio
async def test_oauth_token_endpoint_rejects_bad_secret(client: AsyncClient, enabled_env: None) -> None:
    resp = await client.post(
        "/a2a/v1/oauth/token",
        data={"grant_type": "client_credentials", "client_id": "client-x", "client_secret": "nope"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"


# ---------------------------------------------------------------------------
# The headline AC: send -> poll -> completed artifact -> verify offline
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_send_poll_complete_artifact_verifies_offline(client: AsyncClient, app, enabled_env: None) -> None:  # type: ignore[no-untyped-def]
    c = _A2AClient(client, api_key="key-alice")

    # 1. message/send returns an accepted task.
    send = await c.call("message/send", _A2AClient.text_message("review the auth module"))
    assert send.status_code == 200
    task = send.json()["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "submitted"
    task_id = task["id"]

    # 2. tasks/get before completion still works (degrade-gracefully polling).
    poll = (await c.call("tasks/get", {"id": task_id})).json()["result"]
    assert poll["status"]["state"] in {"submitted", "working"}
    assert poll["artifacts"] == []

    # 3. The worker completes the underlying task.
    await _drive_to_completion(app, task_id, "all 42 tests pass")

    # 4. tasks/get now returns the completed task with a receipt-bearing artifact.
    done = (await c.call("tasks/get", {"id": task_id})).json()["result"]
    assert done["status"]["state"] == "completed"
    assert len(done["artifacts"]) == 1
    artifact = done["artifacts"][0]
    text_part = next(p for p in artifact["parts"] if p["kind"] == "text")
    assert text_part["text"] == "all 42 tests pass"

    # 5. Offline verification: the artifact carries the receipt and the exact
    #    bytes it attests, so the client proves the answer without the node.
    data = next(p for p in artifact["parts"] if p["kind"] == "data")["data"]
    receipt = A2ATaskReceipt.from_dict(data["lineageReceipt"])
    assert verify_task_receipt(receipt, response=data["attested"]).ok


@pytest.mark.anyio
async def test_tampered_completion_artifact_fails_verification(client: AsyncClient, app, enabled_env: None) -> None:  # type: ignore[no-untyped-def]
    """EMPIRICAL: rewriting one byte of the answer is caught by the caller."""
    c = _A2AClient(client, api_key="key-alice")
    task_id = (await c.call("message/send", _A2AClient.text_message("m"))).json()["result"]["id"]
    await _drive_to_completion(app, task_id, "the answer")

    done = (await c.call("tasks/get", {"id": task_id})).json()["result"]
    data = next(p for p in done["artifacts"][0]["parts"] if p["kind"] == "data")["data"]
    receipt = A2ATaskReceipt.from_dict(data["lineageReceipt"])

    tampered = dict(data["attested"])
    tampered["result"] = "the answer."  # one byte added
    result = verify_task_receipt(receipt, response=tampered)
    assert not result.ok
    assert any("content_hash" in e for e in result.errors)


@pytest.mark.anyio
async def test_completion_receipt_is_stable_across_polls(client: AsyncClient, app, enabled_env: None) -> None:  # type: ignore[no-untyped-def]
    """Re-polling a completed task does not grow the audit chain."""
    c = _A2AClient(client, api_key="key-alice")
    task_id = (await c.call("message/send", _A2AClient.text_message("m"))).json()["result"]["id"]
    await _drive_to_completion(app, task_id, "the answer")

    first = (await c.call("tasks/get", {"id": task_id})).json()["result"]
    second = (await c.call("tasks/get", {"id": task_id})).json()["result"]
    r1 = next(p for p in first["artifacts"][0]["parts"] if p["kind"] == "data")["data"]["lineageReceipt"]
    r2 = next(p for p in second["artifacts"][0]["parts"] if p["kind"] == "data")["data"]["lineageReceipt"]
    assert r1["entry_hash"] == r2["entry_hash"]


@pytest.mark.anyio
async def test_authenticated_caller_is_anchored_in_the_audit_chain(client: AsyncClient, app, enabled_env: None) -> None:  # type: ignore[no-untyped-def]
    c = _A2AClient(client, api_key="key-alice")
    task_id = (await c.call("message/send", _A2AClient.text_message("m"))).json()["result"]["id"]
    # The acceptance receipt names the caller via the chain entry the issuer
    # wrote; the handler kept its copy of that receipt.
    stored = app.state.a2a_handler.get_receipt(task_id)
    assert stored is not None
    # The caller rode into the chain: the metadata echoes it and the receipt
    # attests the accepted request.
    result = verify_task_receipt(
        A2ATaskReceipt.from_dict(stored),
        response={"taskId": task_id, "message": "m"},
    )
    assert result.ok


# ---------------------------------------------------------------------------
# JSON-RPC error handling + method dispatch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unknown_method_returns_method_not_found(client: AsyncClient, enabled_env: None) -> None:
    c = _A2AClient(client, api_key="key-alice")
    resp = await c.call("tasks/nonexistent", {})
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32601


@pytest.mark.anyio
async def test_tasks_get_unknown_task_is_a_jsonrpc_error(client: AsyncClient, enabled_env: None) -> None:
    c = _A2AClient(client, api_key="key-alice")
    resp = await c.call("tasks/get", {"id": "does-not-exist"})
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.anyio
async def test_completion_artifact_verifies_and_tamper_rejected_via_cli(
    client: AsyncClient, app, enabled_env: None, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """EMPIRICAL: the shipped ``bernstein a2a verify`` CLI accepts the real
    artifact and rejects a one-byte tamper - the same tool a peer would run.
    """
    from click.testing import CliRunner

    from bernstein.cli.commands.a2a_cmd import a2a_group

    c = _A2AClient(client, api_key="key-alice")
    task_id = (await c.call("message/send", _A2AClient.text_message("m"))).json()["result"]["id"]
    await _drive_to_completion(app, task_id, "the answer")
    done = (await c.call("tasks/get", {"id": task_id})).json()["result"]
    data = next(p for p in done["artifacts"][0]["parts"] if p["kind"] == "data")["data"]

    receipt_file = tmp_path / "receipt.json"
    response_file = tmp_path / "response.json"
    receipt_file.write_text(json.dumps(data["lineageReceipt"]), encoding="utf-8")
    response_file.write_text(json.dumps(data["attested"]), encoding="utf-8")

    runner = CliRunner()
    ok = runner.invoke(a2a_group, ["verify", "--receipt", str(receipt_file), "--response", str(response_file)])
    assert ok.exit_code == 0, ok.output

    tampered = dict(data["attested"])
    tampered["result"] = "the answer."
    response_file.write_text(json.dumps(tampered), encoding="utf-8")
    bad = runner.invoke(a2a_group, ["verify", "--receipt", str(receipt_file), "--response", str(response_file)])
    assert bad.exit_code == 1


@pytest.mark.anyio
async def test_message_stream_returns_sse(client: AsyncClient, enabled_env: None) -> None:
    resp = await client.post(
        "/a2a/v1",
        headers={"X-API-Key": "key-alice", "Accept": "text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/stream",
            "params": _A2AClient.text_message("stream me"),
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # The first SSE event is the accepted task.
    body = resp.text
    first = next(line for line in body.splitlines() if line.startswith("data: "))
    event = json.loads(first[len("data: ") :])
    assert event["result"]["kind"] == "task"
