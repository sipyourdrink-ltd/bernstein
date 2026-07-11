"""MCP server ``bernstein_task_handle`` polling tool (issue #2364).

The tool lets a stateless MCP client fetch a verifiable run handle for a
run it started, reprojected from the on-disk journal and the audit chain
without holding a session. These tests drive the FastMCP tool callable
directly and isolate all state with ``tmp_path``.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.mcp.server import create_mcp_server

pytestmark = pytest.mark.asyncio


def _tool(mcp, name):
    return mcp._tool_manager._tools[name].fn


async def _call(mcp, name, **kwargs):
    raw = await _tool(mcp, name)(**kwargs)
    data = json.loads(raw)
    # Tools are wrapped in the cost-meter envelope by default; unwrap it.
    if isinstance(data, dict) and "_meter" in data and "result" in data:
        return data["result"]
    return data


async def test_task_handle_tool_reprojects_from_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-2364", sdd)
    journal.record("run_started", goal="g")
    journal.record("run_completed", result="done")
    AuditChainStore(sdd / "audit", key=b"k" * 32).log(
        event_type="run.complete", actor="x", resource_type="r", resource_id="1", details={}
    )

    mcp = create_mcp_server(tier="all")
    out = await _call(mcp, "bernstein_task_handle", run_id="run-2364")
    assert out["runId"] == "run-2364"
    assert out["status"] == "completed"
    assert out["journalHead"] == journal.head()
    assert out["chainHead"]
    assert out["receiptHash"]
    assert out["pollToken"]


async def test_task_handle_tool_rejects_path_traversal(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    mcp = create_mcp_server(tier="all")
    out = await _call(mcp, "bernstein_task_handle", run_id="../../etc")
    assert "error" in out


async def test_task_handle_tool_unknown_run(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    mcp = create_mcp_server(tier="all")
    out = await _call(mcp, "bernstein_task_handle", run_id="nope")
    # An unknown run has no journal: the handle is empty-but-valid working.
    assert out["runId"] == "nope"
    assert out["status"] == "working"
