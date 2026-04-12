"""Tests for strict MCP config allowlist (T535).

Covers: MCPRunAllowlist filtering, MCPManager.build_mcp_config_with_allowlist,
seed parsing of mcp_allowlist, and failure/cleanup paths.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.mcp_manager import (
    MCPManager,
    MCPRunAllowlist,
    MCPServerConfig,
)
from bernstein.core.seed import SeedError, parse_seed

# ---------------------------------------------------------------------------
# MCPRunAllowlist unit tests
# ---------------------------------------------------------------------------


class TestMCPRunAllowlist:
    def test_strict_mode_allows_listed(self) -> None:
        al = MCPRunAllowlist(allowed_names=frozenset({"github", "filesystem"}))
        assert al.is_allowed("github") is True
        assert al.is_allowed("filesystem") is True

    def test_strict_mode_blocks_unlisted(self) -> None:
        al = MCPRunAllowlist(allowed_names=frozenset({"github"}))
        assert al.is_allowed("tavily") is False

    def test_permissive_mode_allows_all(self) -> None:
        al = MCPRunAllowlist(allowed_names=frozenset(), mode="permissive")
        assert al.is_allowed("anything") is True
        assert al.is_allowed("other") is True

    def test_filter_server_names_partitions_correctly(self) -> None:
        al = MCPRunAllowlist(allowed_names=frozenset({"github", "filesystem"}))
        allowed, blocked = al.filter_server_names(["github", "filesystem", "tavily", "custom"])
        assert set(allowed) == {"github", "filesystem"}
        assert set(blocked) == {"tavily", "custom"}

    def test_filter_empty_list(self) -> None:
        al = MCPRunAllowlist(allowed_names=frozenset({"github"}))
        allowed, blocked = al.filter_server_names([])
        assert allowed == []
        assert blocked == []

    def test_filter_all_blocked(self) -> None:
        al = MCPRunAllowlist(allowed_names=frozenset())
        allowed, blocked = al.filter_server_names(["a", "b", "c"])
        assert allowed == []
        assert set(blocked) == {"a", "b", "c"}

    def test_to_dict(self) -> None:
        al = MCPRunAllowlist(allowed_names=frozenset({"github", "fs"}), mode="strict")
        d = al.to_dict()
        assert d["mode"] == "strict"
        assert sorted(d["allowed_names"]) == ["fs", "github"]

    def test_frozen_immutable(self) -> None:
        al = MCPRunAllowlist(allowed_names=frozenset({"github"}))
        with pytest.raises(AttributeError):
            al.mode = "permissive"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# MCPManager.build_mcp_config_with_allowlist — healthy path
# ---------------------------------------------------------------------------


class TestBuildMCPConfigWithAllowlist:
    @patch("bernstein.core.protocols.mcp_manager.subprocess.Popen")
    def test_strict_allowlist_filters_servers(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        configs = [
            MCPServerConfig(name="github", command=["npx", "gh-mcp"]),
            MCPServerConfig(name="filesystem", command=["node", "fs.js"]),
            MCPServerConfig(name="tavily", command=["npx", "tavily-mcp"]),
        ]
        mgr = MCPManager(configs)
        mgr.start_all()

        allowlist = MCPRunAllowlist(allowed_names=frozenset({"github", "filesystem"}))
        result = mgr.build_mcp_config_with_allowlist(allowlist=allowlist)

        assert result is not None
        servers = result["mcpServers"]
        assert "github" in servers
        assert "filesystem" in servers
        assert "tavily" not in servers

    @patch("bernstein.core.protocols.mcp_manager.subprocess.Popen")
    def test_permissive_allowlist_allows_all(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        configs = [
            MCPServerConfig(name="github", command=["npx"]),
            MCPServerConfig(name="tavily", command=["npx", "t"]),
        ]
        mgr = MCPManager(configs)
        mgr.start_all()

        al = MCPRunAllowlist(allowed_names=frozenset(), mode="permissive")
        result = mgr.build_mcp_config_with_allowlist(allowlist=al)

        assert result is not None
        assert "github" in result["mcpServers"]
        assert "tavily" in result["mcpServers"]

    def test_no_allowlist_passes_all_alive_through(self) -> None:
        cfg = MCPServerConfig(name="remote", url="http://x/sse", transport="sse")
        mgr = MCPManager([cfg])
        mgr.start_all()

        result = mgr.build_mcp_config_with_allowlist(allowlist=None)
        assert result is not None
        assert "remote" in result["mcpServers"]

    @patch("bernstein.core.protocols.mcp_manager.subprocess.Popen")
    def test_allowlist_all_blocked_returns_none(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="github", command=["npx"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        al = MCPRunAllowlist(allowed_names=frozenset({"only-this-one"}))
        result = mgr.build_mcp_config_with_allowlist(allowlist=al)
        assert result is None


# ---------------------------------------------------------------------------
# MCPManager cleanup / failure path — server not in allowlist at start_all
# ---------------------------------------------------------------------------


class TestAllowlistCleanupPath:
    """Test that blocked servers are not started when the manager respects the allowlist."""

    @patch("bernstein.core.protocols.mcp_manager.subprocess.Popen")
    def test_start_all_then_filter_via_allowlist(self, mock_popen: MagicMock) -> None:
        """Servers can be started but then filtered during config build."""
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        configs = [
            MCPServerConfig(name="allowed_server", command=["echo"]),
            MCPServerConfig(name="blocked_server", command=["echo"]),
        ]
        mgr = MCPManager(configs)
        mgr.start_all()

        al = MCPRunAllowlist(allowed_names=frozenset({"allowed_server"}))
        result = mgr.build_mcp_config_with_allowlist(allowlist=al)

        assert result is not None
        assert "allowed_server" in result["mcpServers"]
        assert "blocked_server" not in result["mcpServers"]

    def test_sse_server_blocked_by_allowlist(self) -> None:
        """SSE servers (no subprocess) are also filtered by allowlist."""
        configs = [
            MCPServerConfig(name="allowed-sse", url="http://ok/sse", transport="sse"),
            MCPServerConfig(name="blocked-sse", url="http://bad/sse", transport="sse"),
        ]
        mgr = MCPManager(configs)
        mgr.start_all()

        al = MCPRunAllowlist(allowed_names=frozenset({"allowed-sse"}))
        result = mgr.build_mcp_config_with_allowlist(allowlist=al)

        assert result is not None
        assert "allowed-sse" in result["mcpServers"]
        assert "blocked-sse" not in result["mcpServers"]


# ---------------------------------------------------------------------------
# seed.py: parse mcp_allowlist
# ---------------------------------------------------------------------------


class TestSeedMCPAllowlist:
    def test_parses_mcp_allowlist(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text(
            'goal: "Test"\nmcp_allowlist: [github, filesystem]\n',
            encoding="utf-8",
        )
        cfg = parse_seed(seed_file)
        assert cfg.mcp_allowlist == ("github", "filesystem")

    def test_mcp_allowlist_defaults_to_none(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text('goal: "No allowlist"\n', encoding="utf-8")
        cfg = parse_seed(seed_file)
        assert cfg.mcp_allowlist is None

    def test_mcp_allowlist_invalid_type_raises(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text(
            'goal: "Test"\nmcp_allowlist: "not-a-list"\n',
            encoding="utf-8",
        )
        with pytest.raises(SeedError):
            parse_seed(seed_file)

    def test_mcp_allowlist_empty_list(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text('goal: "Test"\nmcp_allowlist: []\n', encoding="utf-8")
        cfg = parse_seed(seed_file)
        assert cfg.mcp_allowlist == ()
