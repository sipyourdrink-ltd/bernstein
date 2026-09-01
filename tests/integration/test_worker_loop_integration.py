from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.protocols.mcp.mcp_gateway import MCPGateway
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit_chain import AuditChainStore


class MockSettlement:
    def record_settlement(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_worker_loop_synthetic_journal_integration(tmp_path: Path):
    journal = EventJournal("testrun123", sdd_dir=tmp_path / ".sdd")
    chain = AuditChainStore(tmp_path / ".sdd" / "chain.db")
    from bernstein.core.persistence.action_cache import open_cache

    cache = open_cache(tmp_path, mode="on")

    # Pre-seed the cache with our repeated tool call ActionRecord
    tool_name = "test_tool"
    tool_args = {"action": "stuck"}
    tool_result = {"status": "failed", "error": "retry"}
    cache.record(
        model_id="test-model",
        prompt="system prompt",
        tool_name=tool_name,
        tool_args=tool_args,
        output_text="error text",
        tool_results=tool_result,
        cost_usd=0.0,
    )

    # Retrieve the exact hash we just seeded by running a lookup
    # In action_cache.py, derive_key uses model_id, prompt, tool_name, tool_args
    from bernstein.core.persistence.action_cache import derive_key

    digest = derive_key(model_id="test-model", prompt="system prompt", tool_name=tool_name, tool_args=tool_args)
    content_hash = digest.hex()

    # Now simulate the tool call in MCPGateway
    class MockWAL:
        def append(self, *args, **kwargs):
            return None

    gateway = MCPGateway(
        server_name="test_server",
        upstream_cmd=["dummy"],
        wal_writer=MockWAL(),
        journal=journal,
        audit_chain=chain,
        settlement=MockSettlement(),
        attestation_interlock=None,
    )

    # The default detector threshold is 3. We will run N-1 (2) calls, then N (3).
    # We monkeypatch the gateway to skip actual network and just return our mock
    async def mock_send_request(message, req_id):
        return {"result": "success"}, 10

    async def mock_prepare(*args, **kwargs):
        pass

    gateway._send_request = mock_send_request
    gateway._prepare_tool_dispatch = mock_prepare

    # Simulate a call with cacheScope metadata containing content_hash
    def make_call_message(req_id: int):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": tool_args,
                "_meta": {"cacheScope": {"content_hash": content_hash}},
            },
        }

    # Cycle 1 (no intervention)
    resp1 = await gateway.handle_jsonrpc(make_call_message(1))
    assert resp1["result"] == "success"

    # Cycle 2 (no intervention, N-1 = 2)
    resp2 = await gateway.handle_jsonrpc(make_call_message(2))
    assert resp2["result"] == "success"

    # Cycle 3 (N = 3, threshold reached -> intervention)
    resp3 = await gateway.handle_jsonrpc(make_call_message(3))
    assert "Worker repeated identical action cycle" in resp3["result"]
    assert "LoopDetector: FAILED" in resp3["result"]

    # Verify EventJournal
    from bernstein.core.replay.journal import load_events

    journal_rows = load_events(journal.path).events

    # Replay should see the worker_loop_intervention event
    interventions = [e for e in journal_rows if e.get("event") == "worker_loop_intervention"]
    assert len(interventions) == 1

    event = interventions[0]
    assert event["loop_type"] == "identical"
    assert event["repetition_count"] == 3
    assert event["action_identity"] == content_hash
