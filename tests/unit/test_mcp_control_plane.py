"""Tests for MCP control plane features — T553, T554, T555, T556."""

from __future__ import annotations

import time

from bernstein.core.mcp_manager import (
    MCPCapabilitySnapshot,
    MCPHealthHistory,
    MCPManager,
    MCPServerConfig,
    build_mcp_capability_snapshots,
    explain_mcp_scope_precedence,
    get_oauth_expiry_dashboard,
)


class TestMCPCapabilitySnapshot:
    def test_to_dict_has_required_keys(self) -> None:
        snap = MCPCapabilitySnapshot(
            captured_at=time.time(),
            server_name="my-server",
            alive=True,
            transport="stdio",
            uptime_seconds=60.0,
        )
        d = snap.to_dict()
        assert d["server_name"] == "my-server"
        assert d["alive"] is True
        assert "oauth_expiring_soon" in d

    def test_oauth_expiring_soon_when_close(self) -> None:
        snap = MCPCapabilitySnapshot(
            captured_at=time.time(),
            server_name="s",
            alive=True,
            oauth_expiry=time.time() + 60.0,  # expires in 60s
        )
        assert snap.is_oauth_expiring_soon(threshold_seconds=300.0) is True

    def test_oauth_not_expiring_when_far(self) -> None:
        snap = MCPCapabilitySnapshot(
            captured_at=time.time(),
            server_name="s",
            alive=True,
            oauth_expiry=time.time() + 3600.0,  # expires in 1h
        )
        assert snap.is_oauth_expiring_soon(threshold_seconds=300.0) is False

    def test_no_oauth_expiry_returns_false(self) -> None:
        snap = MCPCapabilitySnapshot(captured_at=time.time(), server_name="s", alive=True)
        assert snap.is_oauth_expiring_soon() is False


class TestMCPHealthHistory:
    def test_record_and_retrieve(self) -> None:
        history = MCPHealthHistory()
        history.record("server-a", alive=True, reason="started")
        history.record("server-a", alive=False, reason="timeout")
        events = history.get_history("server-a")
        assert len(events) == 2
        assert events[0].alive is True
        assert events[1].alive is False
        assert events[1].reason == "timeout"

    def test_empty_history_returns_empty_list(self) -> None:
        history = MCPHealthHistory()
        assert history.get_history("nonexistent") == []

    def test_max_events_enforced(self) -> None:
        history = MCPHealthHistory(max_events=3)
        for i in range(10):
            history.record("s", alive=True)
        assert len(history.get_history("s")) == 3

    def test_to_dict_serialises_all_servers(self) -> None:
        history = MCPHealthHistory()
        history.record("a", alive=True)
        history.record("b", alive=False)
        d = history.to_dict()
        assert "a" in d
        assert "b" in d


class TestExplainMCPScopePrecedence:
    def test_full_chain_ordering(self) -> None:
        chain = explain_mcp_scope_precedence(
            "my-server",
            task_scopes=["read"],
            global_scopes=["read", "write"],
            server_default_scopes=["read", "write", "admin"],
        )
        assert len(chain) == 3
        assert chain[0].source == "task"
        assert chain[1].source == "global"
        assert chain[2].source == "server_default"

    def test_missing_levels_are_omitted(self) -> None:
        chain = explain_mcp_scope_precedence(
            "s",
            task_scopes=None,
            global_scopes=["read"],
            server_default_scopes=None,
        )
        assert len(chain) == 1
        assert chain[0].source == "global"

    def test_empty_chain_when_all_none(self) -> None:
        chain = explain_mcp_scope_precedence("s", None, None, None)
        assert chain == []


class TestBuildMCPCapabilitySnapshots:
    def test_snapshot_per_server(self) -> None:
        manager = MCPManager(
            [
                MCPServerConfig(name="a", command=["echo"], transport="stdio"),
                MCPServerConfig(name="b", url="http://localhost:9000", transport="sse"),
            ]
        )
        snapshots = build_mcp_capability_snapshots(manager)
        assert len(snapshots) == 2
        names = {s.server_name for s in snapshots}
        assert names == {"a", "b"}

    def test_alive_false_when_not_started(self) -> None:
        manager = MCPManager([MCPServerConfig(name="x", command=["echo"])])
        snapshots = build_mcp_capability_snapshots(manager)
        assert snapshots[0].alive is False


class TestGetOAuthExpiryDashboard:
    def test_only_servers_with_oauth_expiry_included(self) -> None:
        snaps = [
            MCPCapabilitySnapshot(captured_at=time.time(), server_name="a", alive=True),
            MCPCapabilitySnapshot(
                captured_at=time.time(), server_name="b", alive=True, oauth_expiry=time.time() + 100
            ),
        ]
        dashboard = get_oauth_expiry_dashboard(snaps)
        assert len(dashboard) == 1
        assert dashboard[0]["server_name"] == "b"

    def test_expiring_soon_flag(self) -> None:
        snaps = [
            MCPCapabilitySnapshot(
                captured_at=time.time(), server_name="c", alive=True, oauth_expiry=time.time() + 60
            )
        ]
        dashboard = get_oauth_expiry_dashboard(snaps)
        assert dashboard[0]["expiring_soon"] is True
