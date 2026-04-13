"""Tests for MCP-005: Lazy MCP server discovery and on-demand startup."""

from __future__ import annotations

import pytest
from bernstein.core.mcp_lazy_discovery import (
    LazyMCPDiscovery,
    LazyServerEntry,
    ServerState,
)
from bernstein.core.mcp_manager import MCPManager, MCPServerConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sse_config() -> MCPServerConfig:
    return MCPServerConfig(name="test-sse", url="http://localhost:9090/sse", transport="sse")


@pytest.fixture()
def stdio_config() -> MCPServerConfig:
    return MCPServerConfig(name="test-stdio", command=["echo", "hi"], transport="stdio")


@pytest.fixture()
def manager() -> MCPManager:
    return MCPManager()


@pytest.fixture()
def discovery(manager: MCPManager) -> LazyMCPDiscovery:
    return LazyMCPDiscovery(manager=manager)


# ---------------------------------------------------------------------------
# Tests — Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_single(self, discovery: LazyMCPDiscovery, sse_config: MCPServerConfig) -> None:
        discovery.register(sse_config)
        assert discovery.get_state("test-sse") == ServerState.REGISTERED
        assert len(discovery.list_entries()) == 1

    def test_register_many(
        self, discovery: LazyMCPDiscovery, sse_config: MCPServerConfig, stdio_config: MCPServerConfig
    ) -> None:
        discovery.register_many([sse_config, stdio_config])
        assert len(discovery.list_entries()) == 2

    def test_register_overwrites_existing(self, discovery: LazyMCPDiscovery, sse_config: MCPServerConfig) -> None:
        discovery.register(sse_config)
        discovery.register(sse_config)
        assert len(discovery.list_entries()) == 1

    def test_unknown_server_state_is_none(self, discovery: LazyMCPDiscovery) -> None:
        assert discovery.get_state("nonexistent") is None


# ---------------------------------------------------------------------------
# Tests — Lazy startup
# ---------------------------------------------------------------------------


class TestLazyStartup:
    def test_ensure_running_unknown_returns_false(self, discovery: LazyMCPDiscovery) -> None:
        assert discovery.ensure_running("nonexistent") is False

    def test_ensure_running_sse(self, discovery: LazyMCPDiscovery, sse_config: MCPServerConfig) -> None:
        discovery.register(sse_config)
        result = discovery.ensure_running("test-sse")
        # SSE servers are marked alive optimistically
        assert result is True
        assert discovery.get_state("test-sse") == ServerState.RUNNING

    def test_ensure_running_idempotent(self, discovery: LazyMCPDiscovery, sse_config: MCPServerConfig) -> None:
        discovery.register(sse_config)
        discovery.ensure_running("test-sse")
        discovery.ensure_running("test-sse")
        entry = discovery.list_entries()[0]
        assert entry.start_count == 2
        assert entry.state == ServerState.RUNNING

    def test_ensure_running_many(
        self, discovery: LazyMCPDiscovery, sse_config: MCPServerConfig, stdio_config: MCPServerConfig
    ) -> None:
        discovery.register(sse_config)
        # Don't register stdio (it would try to launch a real process)
        results = discovery.ensure_running_many(["test-sse", "nonexistent"])
        assert results["test-sse"] is True
        assert results["nonexistent"] is False


# ---------------------------------------------------------------------------
# Tests — Running names & stop idle
# ---------------------------------------------------------------------------


class TestRunningAndIdle:
    def test_get_running_names(self, discovery: LazyMCPDiscovery, sse_config: MCPServerConfig) -> None:
        discovery.register(sse_config)
        assert discovery.get_running_names() == []
        discovery.ensure_running("test-sse")
        assert "test-sse" in discovery.get_running_names()

    def test_stop_idle(self, discovery: LazyMCPDiscovery, sse_config: MCPServerConfig) -> None:
        discovery.register(sse_config)
        discovery.ensure_running("test-sse")
        # Immediately after start, idle_seconds=0 should stop it
        stopped = discovery.stop_idle(idle_seconds=0.0)
        assert "test-sse" in stopped
        assert discovery.get_state("test-sse") == ServerState.REGISTERED

    def test_stop_idle_skips_recently_started(self, discovery: LazyMCPDiscovery, sse_config: MCPServerConfig) -> None:
        discovery.register(sse_config)
        discovery.ensure_running("test-sse")
        stopped = discovery.stop_idle(idle_seconds=9999.0)
        assert stopped == []


# ---------------------------------------------------------------------------
# Tests — Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_dict(self, discovery: LazyMCPDiscovery, sse_config: MCPServerConfig) -> None:
        discovery.register(sse_config)
        d = discovery.to_dict()
        assert "test-sse" in d
        assert d["test-sse"]["state"] == "registered"

    def test_entry_to_dict(self, sse_config: MCPServerConfig) -> None:
        entry = LazyServerEntry(config=sse_config)
        d = entry.to_dict()
        assert d["name"] == "test-sse"
        assert d["state"] == "registered"


# ---------------------------------------------------------------------------
# Tests — Failed startup
# ---------------------------------------------------------------------------


class TestFailedStartup:
    def test_start_failure_sets_failed_state(self, discovery: LazyMCPDiscovery) -> None:
        config = MCPServerConfig(name="broken", command=[], transport="stdio")
        discovery.register(config)
        result = discovery.ensure_running("broken")
        # No command => start fails
        assert result is False
        assert discovery.get_state("broken") == ServerState.FAILED
