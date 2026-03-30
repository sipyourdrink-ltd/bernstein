"""Tests for MCP Gateway Proxy — MCPGateway, GatewayReplay, ToolMetrics, route."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from bernstein.core.mcp_gateway import GatewayReplay, MCPGateway, ToolMetrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wal_writer(tmp_path: Any, run_id: str = "test-gw") -> Any:
    from pathlib import Path

    from bernstein.core.wal import WALWriter

    sdd = Path(tmp_path) / ".sdd"
    sdd.mkdir(exist_ok=True)
    return WALWriter(run_id=run_id, sdd_dir=sdd)


def _make_wal_with_calls(
    tmp_path: Any,
    run_id: str,
    calls: list[dict[str, Any]],
) -> None:
    """Write mcp_tool_call entries to a WAL file."""
    from pathlib import Path

    from bernstein.core.wal import WALWriter

    sdd = Path(tmp_path) / ".sdd"
    sdd.mkdir(exist_ok=True)
    writer = WALWriter(run_id=run_id, sdd_dir=sdd)
    for call in calls:
        writer.append(
            decision_type="mcp_tool_call",
            inputs=call["inputs"],
            output=call["output"],
            actor="mcp_gateway",
        )


# ---------------------------------------------------------------------------
# TestToolMetrics
# ---------------------------------------------------------------------------


class TestToolMetrics:
    def test_record_increments_total(self) -> None:
        m = ToolMetrics(tool_name="read_file")
        m.record(10.0)
        m.record(20.0)
        assert m.total_calls == 2

    def test_record_error_flag(self) -> None:
        m = ToolMetrics(tool_name="read_file")
        m.record(10.0, error=True)
        m.record(20.0, error=False)
        assert m.error_count == 1

    def test_to_dict_structure(self) -> None:
        m = ToolMetrics(tool_name="write_file")
        for latency in [10.0, 20.0, 30.0, 40.0, 50.0]:
            m.record(latency)
        d = m.to_dict()
        assert d["tool_name"] == "write_file"
        assert d["total_calls"] == 5
        assert d["error_count"] == 0
        assert d["error_rate"] == 0.0
        assert "latency_p50_ms" in d
        assert "latency_p90_ms" in d
        assert "latency_p99_ms" in d

    def test_error_rate_calculation(self) -> None:
        m = ToolMetrics(tool_name="t")
        m.record(5.0, error=True)
        m.record(5.0)
        m.record(5.0)
        m.record(5.0, error=True)
        assert m.to_dict()["error_rate"] == 0.5

    def test_empty_metrics_zero_latencies(self) -> None:
        m = ToolMetrics(tool_name="t")
        d = m.to_dict()
        assert d["latency_p50_ms"] == 0.0
        assert d["error_rate"] == 0.0


# ---------------------------------------------------------------------------
# TestGatewayReplay
# ---------------------------------------------------------------------------


class TestGatewayReplay:
    def test_indexes_mcp_tool_calls(self, tmp_path: Any) -> None:
        _make_wal_with_calls(
            tmp_path,
            "replay-run",
            [
                {
                    "inputs": {"method": "tools/call", "tool_name": "read_file", "arguments": {}, "request_id": 1},
                    "output": {"result": {"content": [{"text": "hello"}]}, "error": None, "latency_ms": 12.5},
                }
            ],
        )
        from pathlib import Path

        replay = GatewayReplay(run_id="replay-run", sdd_dir=Path(tmp_path) / ".sdd")
        assert replay.indexed_count == 1

    def test_find_response_returns_output(self, tmp_path: Any) -> None:
        _make_wal_with_calls(
            tmp_path,
            "replay-run",
            [
                {
                    "inputs": {"method": "tools/call", "tool_name": "list_tools", "arguments": {}, "request_id": 2},
                    "output": {"result": {"tools": []}, "error": None, "latency_ms": 5.0},
                }
            ],
        )
        from pathlib import Path

        replay = GatewayReplay(run_id="replay-run", sdd_dir=Path(tmp_path) / ".sdd")
        result = replay.find_response("tools/call", {"name": "list_tools"})
        assert result is not None
        assert result["result"] == {"tools": []}

    def test_find_response_returns_none_for_unknown(self, tmp_path: Any) -> None:
        _make_wal_with_calls(tmp_path, "replay-run", [])
        from pathlib import Path

        replay = GatewayReplay(run_id="replay-run", sdd_dir=Path(tmp_path) / ".sdd")
        assert replay.find_response("tools/call", {"name": "nonexistent_tool"}) is None

    def test_last_recorded_response_wins(self, tmp_path: Any) -> None:
        """When the same tool is called twice, the last response is indexed."""
        _make_wal_with_calls(
            tmp_path,
            "replay-run",
            [
                {
                    "inputs": {"method": "tools/call", "tool_name": "read_file", "arguments": {}, "request_id": 1},
                    "output": {"result": "first", "error": None, "latency_ms": 1.0},
                },
                {
                    "inputs": {"method": "tools/call", "tool_name": "read_file", "arguments": {}, "request_id": 2},
                    "output": {"result": "second", "error": None, "latency_ms": 2.0},
                },
            ],
        )
        from pathlib import Path

        replay = GatewayReplay(run_id="replay-run", sdd_dir=Path(tmp_path) / ".sdd")
        result = replay.find_response("tools/call", {"name": "read_file"})
        assert result is not None
        assert result["result"] == "second"

    def test_handles_missing_wal_gracefully(self, tmp_path: Any) -> None:
        """GatewayReplay with a nonexistent WAL does not raise; index is empty."""
        from pathlib import Path

        replay = GatewayReplay(run_id="does-not-exist", sdd_dir=Path(tmp_path) / ".sdd")
        assert replay.indexed_count == 0
        assert replay.find_response("tools/call", {"name": "x"}) is None


# ---------------------------------------------------------------------------
# TestMCPGateway — replay mode
# ---------------------------------------------------------------------------


class TestMCPGatewayReplayMode:
    @pytest.mark.asyncio
    async def test_replay_returns_recorded_result(self, tmp_path: Any) -> None:
        _make_wal_with_calls(
            tmp_path,
            "source-run",
            [
                {
                    "inputs": {"method": "tools/call", "tool_name": "search", "arguments": {}, "request_id": 1},
                    "output": {"result": {"hits": 42}, "error": None, "latency_ms": 8.0},
                }
            ],
        )
        from pathlib import Path

        sdd = Path(tmp_path) / ".sdd"
        replay = GatewayReplay(run_id="source-run", sdd_dir=sdd)
        writer = _make_wal_writer(tmp_path, run_id="replay-session")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer, replay=replay)

        resp = await gw.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": "search", "arguments": {}}}
        )
        assert resp is not None
        assert resp["id"] == 99
        assert resp["result"] == {"hits": 42}

    @pytest.mark.asyncio
    async def test_replay_returns_error_for_unknown_call(self, tmp_path: Any) -> None:
        from pathlib import Path

        sdd = Path(tmp_path) / ".sdd"
        replay = GatewayReplay(run_id="empty-run", sdd_dir=sdd)
        writer = _make_wal_writer(tmp_path, run_id="replay-session")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer, replay=replay)

        resp = await gw.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "missing_tool"}}
        )
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32000

    @pytest.mark.asyncio
    async def test_replay_notification_returns_none(self, tmp_path: Any) -> None:
        from pathlib import Path

        sdd = Path(tmp_path) / ".sdd"
        replay = GatewayReplay(run_id="empty-run", sdd_dir=sdd)
        writer = _make_wal_writer(tmp_path, run_id="replay-session")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer, replay=replay)

        # Notifications have no "id" key
        resp = await gw.handle_jsonrpc({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        assert resp is None


# ---------------------------------------------------------------------------
# TestMCPGateway — live mode (mocked upstream)
# ---------------------------------------------------------------------------


class TestMCPGatewayLiveMode:
    @pytest.mark.asyncio
    async def test_live_proxy_records_to_wal(self, tmp_path: Any) -> None:
        """handle_jsonrpc in live mode writes a WAL entry and returns the response."""
        from pathlib import Path

        from bernstein.core.wal import WALReader

        sdd = Path(tmp_path) / ".sdd"
        writer = _make_wal_writer(tmp_path, run_id="live-session")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer)

        fake_response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }

        # When _send_upstream is called, resolve the future that handle_jsonrpc created
        async def _mock_send(msg: dict[str, Any]) -> None:
            req_id = msg.get("id")
            fut = gw._pending.get(req_id)
            if fut and not fut.done():
                fut.set_result(fake_response)

        with patch.object(gw, "_send_upstream", side_effect=_mock_send):
            resp = await gw.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "read_file", "arguments": {"path": "/tmp/x"}},
                }
            )

        assert resp is not None
        assert resp["result"]["content"][0]["text"] == "ok"

        reader = WALReader(run_id="live-session", sdd_dir=sdd)
        entries = list(reader.iter_entries())
        assert len(entries) == 1
        assert entries[0].decision_type == "mcp_tool_call"
        assert entries[0].inputs["tool_name"] == "read_file"
        assert "latency_ms" in entries[0].output

    @pytest.mark.asyncio
    async def test_live_proxy_updates_metrics(self, tmp_path: Any) -> None:
        """Tool metrics are updated after each call."""
        writer = _make_wal_writer(tmp_path, run_id="metrics-session")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer)

        fake_response: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "result": "done"}

        async def _mock_send(msg: dict[str, Any]) -> None:
            req_id = msg.get("id")
            fut = gw._pending.get(req_id)
            if fut and not fut.done():
                fut.set_result(fake_response)

        with patch.object(gw, "_send_upstream", side_effect=_mock_send):
            await gw.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "run_test"}})

        metrics = gw.get_metrics()
        assert "tools/call:run_test" in metrics
        assert metrics["tools/call:run_test"]["total_calls"] == 1

    @pytest.mark.asyncio
    async def test_notification_not_recorded_to_wal(self, tmp_path: Any) -> None:
        """Notifications (no id) are forwarded but not WAL-recorded."""
        from pathlib import Path

        from bernstein.core.wal import WALReader

        sdd = Path(tmp_path) / ".sdd"
        writer = _make_wal_writer(tmp_path, run_id="notif-session")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer)

        with patch.object(gw, "_send_upstream", new_callable=AsyncMock):
            result = await gw.handle_jsonrpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert result is None

        wal_path = sdd / "runtime" / "wal" / "notif-session.wal.jsonl"
        if wal_path.exists():
            reader = WALReader(run_id="notif-session", sdd_dir=sdd)
            entries = [e for e in reader.iter_entries() if e.decision_type == "mcp_tool_call"]
            assert entries == []

    @pytest.mark.asyncio
    async def test_error_response_recorded_in_wal(self, tmp_path: Any) -> None:
        """When upstream returns an error, it is still recorded in the WAL."""
        from pathlib import Path

        from bernstein.core.wal import WALReader

        sdd = Path(tmp_path) / ".sdd"
        writer = _make_wal_writer(tmp_path, run_id="error-session")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer)

        error_response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 5,
            "error": {"code": -32601, "message": "Method not found"},
        }

        async def _mock_send(msg: dict[str, Any]) -> None:
            req_id = msg.get("id")
            fut = gw._pending.get(req_id)
            if fut and not fut.done():
                fut.set_result(error_response)

        with patch.object(gw, "_send_upstream", side_effect=_mock_send):
            await gw.handle_jsonrpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "bad_tool"}})

        reader = WALReader(run_id="error-session", sdd_dir=sdd)
        entries = list(reader.iter_entries())
        assert len(entries) == 1
        assert entries[0].output["error"] is not None

        metrics = gw.get_metrics()
        assert metrics["tools/call:bad_tool"]["error_count"] == 1


# ---------------------------------------------------------------------------
# TestGatewayMetricsRoute
# ---------------------------------------------------------------------------


class TestGatewayMetricsRoute:
    def test_returns_inactive_when_no_gateway(self) -> None:
        """GET /gateway/metrics returns {active: False} when no gateway is set."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from bernstein.core.routes.gateway import router

        app = FastAPI()
        app.state.mcp_gateway = None
        app.include_router(router)

        client = TestClient(app)
        resp = client.get("/gateway/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False
        assert data["metrics"] == {}

    def test_returns_active_with_gateway_metrics(self, tmp_path: Any) -> None:
        """GET /gateway/metrics returns {active: True, metrics: ...} when gateway present."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from bernstein.core.routes.gateway import router

        writer = _make_wal_writer(tmp_path, run_id="route-test")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer)
        gw._metrics["tools/call:read_file"] = ToolMetrics(
            tool_name="tools/call:read_file",
            total_calls=3,
            error_count=1,
            latency_samples=[10.0, 20.0, 30.0],
        )

        app = FastAPI()
        app.state.mcp_gateway = gw
        app.include_router(router)

        client = TestClient(app)
        resp = client.get("/gateway/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert "tools/call:read_file" in data["metrics"]
        assert data["metrics"]["tools/call:read_file"]["total_calls"] == 3
        assert data["metrics"]["tools/call:read_file"]["error_count"] == 1
