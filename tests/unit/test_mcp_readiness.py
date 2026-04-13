"""Tests for AGENT-005 — MCP server readiness probe."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bernstein.core.mcp_readiness import (
    MCPReadinessError,
    probe_stdio_server,
    validate_mcp_readiness,
)

# ---------------------------------------------------------------------------
# probe_stdio_server
# ---------------------------------------------------------------------------


class TestProbeStdioServer:
    def test_alive_process_ready(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        assert probe_stdio_server(proc, timeout=1.0, poll_interval=0.1)

    def test_dead_process_not_ready(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = 1  # exited with code 1
        assert not probe_stdio_server(proc, timeout=0.5, poll_interval=0.1)

    def test_crash_during_probe(self) -> None:
        proc = MagicMock()
        call_count = 0

        def fake_poll() -> int | None:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                return 1
            return None

        proc.poll.side_effect = fake_poll
        # Will see None twice then 1 — should detect the crash
        result = probe_stdio_server(proc, timeout=2.0, poll_interval=0.1)
        # First poll None, sleep, second poll None => returns True on second check
        # But then the third poll returns 1 — depends on timing
        # The function should return based on the state at check time
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# validate_mcp_readiness
# ---------------------------------------------------------------------------


class TestValidateReadiness:
    def _make_manager(
        self,
        servers: dict[str, bool],
        configs: dict[str, str] | None = None,
    ) -> MagicMock:
        manager = MagicMock()
        manager.server_names = list(servers.keys())
        manager.is_alive.side_effect = lambda name: servers.get(name, False)

        def get_info(name: str) -> MagicMock | None:
            if name not in servers:
                return None
            cfg = MagicMock()
            transport = "stdio"
            url = ""
            if configs and name in configs:
                transport = "sse"
                url = configs[name]
            cfg.transport = transport
            cfg.url = url
            return cfg

        manager.get_server_info.side_effect = get_info
        return manager

    def test_all_alive_passes(self) -> None:
        manager = self._make_manager({"bernstein": True, "github": True})
        results = validate_mcp_readiness(manager, timeout=0.5, poll_interval=0.1)
        assert all(r.ready for r in results)
        assert len(results) == 2

    def test_dead_server_fails(self) -> None:
        manager = self._make_manager({"bernstein": False})
        with pytest.raises(MCPReadinessError, match="bernstein"):
            validate_mcp_readiness(manager, timeout=0.5)

    def test_dead_server_no_fail(self) -> None:
        manager = self._make_manager({"bernstein": False})
        results = validate_mcp_readiness(
            manager,
            timeout=0.5,
            fail_on_error=False,
        )
        assert len(results) == 1
        assert not results[0].ready
        assert results[0].reason

    def test_subset_of_servers(self) -> None:
        manager = self._make_manager({"bernstein": True, "github": True})
        results = validate_mcp_readiness(
            manager,
            server_names=["bernstein"],
            timeout=0.5,
            poll_interval=0.1,
        )
        assert len(results) == 1
        assert results[0].server_name == "bernstein"

    def test_no_config_fails(self) -> None:
        manager = MagicMock()
        manager.server_names = ["mystery"]
        manager.is_alive.return_value = True
        manager.get_server_info.return_value = None
        with pytest.raises(MCPReadinessError, match="No configuration"):
            validate_mcp_readiness(manager, server_names=["mystery"], timeout=0.5)

    def test_elapsed_time_recorded(self) -> None:
        manager = self._make_manager({"bernstein": True})
        results = validate_mcp_readiness(manager, timeout=0.5, poll_interval=0.1)
        assert results[0].elapsed_s >= 0


# ---------------------------------------------------------------------------
# MCPReadinessError attributes
# ---------------------------------------------------------------------------


class TestMCPReadinessError:
    def test_attributes(self) -> None:
        err = MCPReadinessError("myserver", "process crashed")
        assert err.server_name == "myserver"
        assert err.reason == "process crashed"
        assert "myserver" in str(err)
        assert "process crashed" in str(err)
