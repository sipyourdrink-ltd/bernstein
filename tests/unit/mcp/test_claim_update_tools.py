"""Native MCP claim/update tools (#2555).

``bernstein_claim`` and ``bernstein_update`` put the worker-inbound verbs on
the native MCP surface: a claim returns a verifiable claim receipt and a
progress update returns a signed journal entry. Both call the task server
over HTTP via the existing stateless action-handler pattern. These tests
mock the HTTP client so they assert the tool wiring, not the server.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bernstein.core.protocols.mcp.tool_tiers import tool_in_tier


def _mock_client(payload: dict[str, object]) -> AsyncMock:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=payload)
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=mock_response)
    return client


def _tool_names(tier: str) -> set[str]:
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052", tier=tier)
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


# ---------------------------------------------------------------------------
# Tier registration
# ---------------------------------------------------------------------------


def test_claim_and_update_are_standard_tier() -> None:
    assert tool_in_tier("bernstein_claim", "standard")
    assert tool_in_tier("bernstein_update", "standard")
    # Not advertised in the core budget.
    assert not tool_in_tier("bernstein_claim", "core")
    assert not tool_in_tier("bernstein_update", "core")


def test_tools_registered_in_standard_not_core() -> None:
    standard = _tool_names("standard")
    assert {"bernstein_claim", "bernstein_update"} <= standard
    core = _tool_names("core")
    assert "bernstein_claim" not in core
    assert "bernstein_update" not in core


# ---------------------------------------------------------------------------
# bernstein_claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bernstein_claim_posts_to_claim_receipt_route() -> None:
    from bernstein.mcp.server import create_mcp_server

    receipt = {"taskId": "t1", "granted": True, "receiptHash": "abc", "signature": "sig"}
    client = _mock_client(receipt)
    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=client):
        result = await mcp.call_tool(
            "bernstein_claim",
            {"claimer_id": "worker-1", "role": "backend", "completed_ids": ["t0"]},
        )

    call_url = client.post.call_args[0][0]
    assert call_url.endswith("/tasks/claim-receipt")
    body = client.post.call_args.kwargs["json"]
    assert body["claimer_id"] == "worker-1"
    assert body["role"] == "backend"
    assert body["completed_ids"] == ["t0"]
    text = result[0][0].text  # type: ignore[index]
    assert "t1" in text


@pytest.mark.asyncio
async def test_bernstein_claim_rejects_bad_task_id_pattern() -> None:
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")
    result = await mcp.call_tool("bernstein_claim", {"claimer_id": "bad id with spaces/../"})
    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    assert "error" in parsed


# ---------------------------------------------------------------------------
# bernstein_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bernstein_update_posts_to_mailbox_route() -> None:
    from bernstein.mcp.server import create_mcp_server

    entry = {"seq": 0, "task_id": "t1", "entry_hash": "hmac-sha256:x", "signature": "sig"}
    client = _mock_client(entry)
    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=client):
        result = await mcp.call_tool(
            "bernstein_update",
            {"task_id": "t1", "body": "halfway done", "sender": "worker-1"},
        )

    call_url = client.post.call_args[0][0]
    assert call_url.endswith("/tasks/t1/messages")
    body = client.post.call_args.kwargs["json"]
    assert body["sender"] == "worker-1"
    assert body["kind"] == "finding"
    assert body["body"] == "halfway done"
    text = result[0][0].text  # type: ignore[index]
    assert "hmac-sha256" in text


@pytest.mark.asyncio
async def test_bernstein_update_rejects_unknown_kind() -> None:
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")
    result = await mcp.call_tool(
        "bernstein_update",
        {"task_id": "t1", "body": "x", "sender": "w", "kind": "chit_chat"},
    )
    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    assert "error" in parsed
