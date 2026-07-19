"""``POST /a2a/tasks/send`` returns a lineage receipt (#2609).

End-to-end over the real ASGI app: a peer sends a task, gets a response, and
verifies offline that the response it holds is the one the node recorded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.protocols.a2a.receipt import (
    A2ATaskReceipt,
    verify_task_receipt,
)
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    return create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _attested(body: dict[str, Any]) -> dict[str, Any]:
    """Return the response payload the receipt attests to (receipt excluded)."""
    return {k: v for k, v in body.items() if k != "receipt"}


# ---------------------------------------------------------------------------
# AC: every inbound response carries a receipt
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_send_task_response_carries_a_receipt(client: AsyncClient) -> None:
    response = await client.post(
        "/a2a/tasks/send",
        json={"sender": "peer.example", "message": "review the auth module"},
    )
    assert response.status_code == 201
    body = response.json()

    assert body.get("receipt") is not None, "inbound response must carry a lineage receipt"
    receipt = body["receipt"]
    for key in ("entry_hash", "content_hash", "operator_hmac", "head_signature", "kid"):
        assert key in receipt, f"receipt missing {key!r}"


@pytest.mark.anyio
async def test_returned_receipt_verifies_against_the_response(client: AsyncClient) -> None:
    """The peer verifies offline, holding only what the wire gave it."""
    response = await client.post(
        "/a2a/tasks/send",
        json={"sender": "peer.example", "message": "review the auth module"},
    )
    body = response.json()

    receipt = A2ATaskReceipt.from_dict(body["receipt"])
    result = verify_task_receipt(receipt, response=_attested(body))

    assert result.ok, result.errors


@pytest.mark.anyio
async def test_tampered_response_fails_verification(client: AsyncClient) -> None:
    """EMPIRICAL: rewriting the answer in flight is detected by the caller."""
    response = await client.post(
        "/a2a/tasks/send",
        json={"sender": "peer.example", "message": "review the auth module"},
    )
    body = response.json()
    receipt = A2ATaskReceipt.from_dict(body["receipt"])

    tampered = _attested(body)
    tampered["message"] = "review the auth module."  # one byte added

    result = verify_task_receipt(receipt, response=tampered)

    assert not result.ok
    assert any("content_hash" in err for err in result.errors)


@pytest.mark.anyio
async def test_receipt_kid_matches_the_published_key_set(client: AsyncClient) -> None:
    """The receipt names a key the node actually publishes."""
    response = await client.post(
        "/a2a/tasks/send",
        json={"sender": "peer.example", "message": "m"},
    )
    receipt = response.json()["receipt"]

    assert receipt["kid"]
    assert receipt["head_signature"]["public_key_jwk"]["kty"] == "OKP"


@pytest.mark.anyio
async def test_receipt_is_recoverable_from_the_handler(client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    """The node keeps its side of the receipt, so it can be re-served."""
    response = await client.post(
        "/a2a/tasks/send",
        json={"sender": "peer.example", "message": "m"},
    )
    body = response.json()

    stored = app.state.a2a_handler.get_receipt(body["id"])

    assert stored is not None
    assert stored["entry_hash"] == body["receipt"]["entry_hash"]


@pytest.mark.anyio
async def test_two_identical_tasks_produce_distinct_but_valid_receipts(client: AsyncClient) -> None:
    """Distinct tasks chain to distinct lineage positions, each verifiable.

    Determinism is asserted at the projection level in
    ``tests/unit/test_a2a_receipt.py``; over HTTP each task is a genuinely
    new inbound event, so the chain anchors must differ while both remain
    independently verifiable.
    """
    payload = {"sender": "peer.example", "message": "same message"}
    first = (await client.post("/a2a/tasks/send", json=payload)).json()
    second = (await client.post("/a2a/tasks/send", json=payload)).json()

    assert first["receipt"]["entry_hash"] != second["receipt"]["entry_hash"]
    assert verify_task_receipt(A2ATaskReceipt.from_dict(first["receipt"]), response=_attested(first)).ok
    assert verify_task_receipt(A2ATaskReceipt.from_dict(second["receipt"]), response=_attested(second)).ok


@pytest.mark.anyio
async def test_get_task_still_works_without_a_receipt(client: AsyncClient) -> None:
    """Reads are unchanged: the receipt rides on the inbound write path."""
    created = (await client.post("/a2a/tasks/send", json={"sender": "p", "message": "m"})).json()

    fetched = await client.get(f"/a2a/tasks/{created['id']}")

    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]
