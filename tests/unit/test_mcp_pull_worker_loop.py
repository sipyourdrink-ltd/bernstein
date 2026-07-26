"""End-to-end MCP pull-worker loop (#2555, AC1 + AC6).

An MCP-only agent runs the whole worker loop over MCP alone:
``bernstein_claim`` (signed claim receipt) -> N x ``bernstein_update``
(signed journal entries) -> ``bernstein_complete`` (completion). Every step
returns an object that verifies offline against the audit chain the
``audit verify`` path walks. This test drives the real MCP tool handlers with
their HTTP client bridged to the in-process ASGI app, so it exercises the
tool code path, not a re-implementation.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.communication.task_mailbox import TaskMailbox, verify_against_chain
from bernstein.core.protocols.mcp.claim_receipt import ClaimReceipt, verify_claim_receipt
from bernstein.core.server import create_app
from bernstein.core.tasks.claim import Backlog, BacklogEntry

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

_SERVER_URL = "http://localhost:8052"


def _unwrap(text: str) -> dict:
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    return parsed


async def test_full_claim_update_complete_loop_verifies_offline(tmp_path: Path) -> None:
    from bernstein.mcp.server import create_mcp_server

    app = create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")

    # An MCP agent bridges its HTTP client to the running task server; here we
    # bind it to the in-process ASGI app so the loop runs over the real routes.
    def _bridged_client(**_kwargs: object) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url=_SERVER_URL)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_SERVER_URL) as http:
        create = await http.post(
            "/tasks",
            json={"title": "ship it", "description": "do the thing", "role": "backend"},
        )
        assert create.status_code == 201, create.text
        task_id = str(create.json()["id"])

    # The claim path claims from the shared JSON backlog wired by create_app.
    Backlog.write(app.state.claim_backlog_path, [BacklogEntry(id=task_id, role="backend")])

    mcp = create_mcp_server(server_url=_SERVER_URL)

    from unittest.mock import patch

    with patch("bernstein.mcp.server.httpx.AsyncClient", side_effect=_bridged_client):
        # 1. CLAIM -> signed claim receipt.
        claim_result = await mcp.call_tool("bernstein_claim", {"claimer_id": "worker-1", "role": "backend"})
        claim_wire = _unwrap(claim_result[0][0].text)  # type: ignore[index]
        assert claim_wire["granted"] is True
        assert claim_wire["taskId"] == task_id

        # 2. N x UPDATE -> signed journal entries.
        for note in ("25% done", "50% done", "90% done"):
            upd = await mcp.call_tool(
                "bernstein_update",
                {"task_id": task_id, "body": note, "sender": "worker-1"},
            )
            upd_wire = _unwrap(upd[0][0].text)  # type: ignore[index]
            assert upd_wire["entry_hash"].startswith("hmac-sha256:")

        # 3. COMPLETE -> the worker reports what it produced. The approval
        # verb is not a completion path: a task the worker is executing is
        # not in an approval state, so bernstein_approve refuses it.
        refused = await mcp.call_tool("bernstein_approve", {"task_id": task_id, "note": "looks good"})
        refused_wire = _unwrap(refused[0][0].text)  # type: ignore[index]
        assert refused_wire["error"] == "task_not_awaiting_approval"

        done = await mcp.call_tool(
            "bernstein_complete",
            {"task_id": task_id, "result_summary": "shipped it"},
        )
        done_wire = _unwrap(done[0][0].text)  # type: ignore[index]
        assert done_wire["task_id"] == task_id
        assert done_wire["status"] == "done"

    # Every step verifies offline against the audit chain (AC5, AC6).
    receipt = ClaimReceipt.from_wire(claim_wire)
    rows = [e.to_dict() for e in Backlog.load(app.state.claim_backlog_path).entries]
    ok, reason = verify_claim_receipt(receipt, rows, app.state.audit_chain)
    assert ok, reason

    # The mailbox journal cross-verifies against the audit chain end to end,
    # reopened from disk with no in-memory session (offline, stateless).
    from bernstein.core.server.dashboard_tokens import resolve_dashboard_hmac_key

    reopened = TaskMailbox(
        app.state.task_mailbox.path,
        hmac_key=resolve_dashboard_hmac_key(app.state.sdd_dir),
    )
    mb_ok, problems = verify_against_chain(reopened, app.state.audit_chain)
    assert mb_ok, problems

    # The whole audit chain verifies (what ``bernstein audit verify`` walks).
    chain_ok, errors = app.state.audit_chain.verify()
    assert chain_ok, errors
