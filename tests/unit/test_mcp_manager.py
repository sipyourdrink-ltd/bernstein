"""Tests for MCP server lifecycle manager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.mcp_manager import (
    MCPManager,
    MCPServerConfig,
    parse_server_configs,
)

# ---------------------------------------------------------------------------
# MCPServerConfig
# ---------------------------------------------------------------------------


class TestMCPServerConfig:
    """Tests for MCPServerConfig dataclass."""

    def test_stdio_defaults(self) -> None:
        cfg = MCPServerConfig(name="test", command=["npx", "-y", "test-mcp"])
        assert cfg.transport == "stdio"
        assert cfg.url == ""
        assert cfg.env == {}

    def test_sse_config(self) -> None:
        cfg = MCPServerConfig(
            name="remote",
            url="http://localhost:9090/sse",
            transport="sse",
        )
        assert cfg.transport == "sse"
        assert cfg.url == "http://localhost:9090/sse"

    def test_frozen_immutability(self) -> None:
        cfg = MCPServerConfig(name="test", command=["echo"])
        with pytest.raises(AttributeError):
            cfg.name = "changed"  # type: ignore[misc]

    def test_to_mcp_config_entry_stdio(self) -> None:
        cfg = MCPServerConfig(
            name="github",
            command=["npx", "-y", "@anthropic/github-mcp"],
            env={"GITHUB_TOKEN": "tok123"},
        )
        entry = cfg.to_mcp_config_entry()
        assert entry["command"] == "npx"
        assert entry["args"] == ["-y", "@anthropic/github-mcp"]
        assert entry["env"] == {"GITHUB_TOKEN": "tok123"}

    def test_to_mcp_config_entry_sse(self) -> None:
        cfg = MCPServerConfig(
            name="remote",
            url="http://localhost:9090/sse",
            transport="sse",
        )
        entry = cfg.to_mcp_config_entry()
        assert entry["url"] == "http://localhost:9090/sse"
        assert "command" not in entry

    def test_to_mcp_config_entry_empty_command(self) -> None:
        cfg = MCPServerConfig(name="empty", command=[])
        entry = cfg.to_mcp_config_entry()
        assert entry == {}

    def test_to_mcp_config_entry_no_env(self) -> None:
        cfg = MCPServerConfig(name="basic", command=["echo", "hello"])
        entry = cfg.to_mcp_config_entry()
        assert "env" not in entry

    def test_to_mcp_config_entry_sse_with_env(self) -> None:
        cfg = MCPServerConfig(
            name="remote",
            url="http://localhost:9090/sse",
            transport="sse",
            env={"API_KEY": "secret"},
        )
        entry = cfg.to_mcp_config_entry()
        assert entry["url"] == "http://localhost:9090/sse"
        assert entry["env"] == {"API_KEY": "secret"}


# ---------------------------------------------------------------------------
# parse_server_configs
# ---------------------------------------------------------------------------


class TestParseServerConfigs:
    """Tests for parsing raw YAML dicts into MCPServerConfig."""

    def test_parses_stdio_server(self) -> None:
        raw = {
            "github": {
                "command": "npx",
                "args": ["-y", "@anthropic/github-mcp"],
                "env": {"GITHUB_TOKEN": "tok"},
            }
        }
        configs = parse_server_configs(raw)
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.name == "github"
        assert cfg.command == ["npx", "-y", "@anthropic/github-mcp"]
        assert cfg.transport == "stdio"
        assert cfg.env == {"GITHUB_TOKEN": "tok"}

    def test_parses_sse_server(self) -> None:
        raw = {
            "custom-api": {
                "url": "http://localhost:9090/sse",
                "transport": "sse",
            }
        }
        configs = parse_server_configs(raw)
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.name == "custom-api"
        assert cfg.transport == "sse"
        assert cfg.url == "http://localhost:9090/sse"

    def test_infers_sse_from_url(self) -> None:
        raw = {"api": {"url": "http://example.com/sse"}}
        configs = parse_server_configs(raw)
        assert configs[0].transport == "sse"

    def test_parses_multiple_servers(self) -> None:
        raw = {
            "github": {"command": "npx", "args": ["-y", "gh-mcp"]},
            "filesystem": {"command": ["node", "fs-server.js"]},
        }
        configs = parse_server_configs(raw)
        assert len(configs) == 2
        names = {c.name for c in configs}
        assert names == {"github", "filesystem"}

    def test_command_as_list(self) -> None:
        raw = {"fs": {"command": ["node", "server.js"], "args": ["--port", "8080"]}}
        configs = parse_server_configs(raw)
        assert configs[0].command == ["node", "server.js", "--port", "8080"]

    def test_command_as_string(self) -> None:
        raw = {"simple": {"command": "my-server"}}
        configs = parse_server_configs(raw)
        assert configs[0].command == ["my-server"]

    def test_empty_input(self) -> None:
        configs = parse_server_configs({})
        assert configs == []

    def test_missing_env(self) -> None:
        raw = {"basic": {"command": "echo"}}
        configs = parse_server_configs(raw)
        assert configs[0].env == {}


# ---------------------------------------------------------------------------
# MCPManager lifecycle
# ---------------------------------------------------------------------------


class TestMCPManagerLifecycle:
    """Tests for MCPManager start/stop lifecycle."""

    def test_init_empty(self) -> None:
        mgr = MCPManager()
        assert mgr.configs == []
        assert mgr.server_names == []

    def test_init_with_configs(self) -> None:
        configs = [
            MCPServerConfig(name="a", command=["echo"]),
            MCPServerConfig(name="b", url="http://x", transport="sse"),
        ]
        mgr = MCPManager(configs)
        assert len(mgr.configs) == 2
        assert mgr.server_names == ["a", "b"]

    def test_add_config(self) -> None:
        mgr = MCPManager()
        mgr.add_config(MCPServerConfig(name="new", command=["echo"]))
        assert len(mgr.configs) == 1
        assert mgr.server_names == ["new"]

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_start_all_stdio(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="test", command=["npx", "-y", "test-mcp"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        assert call_args.args[0] == ["npx", "-y", "test-mcp"]
        assert mgr.is_alive("test") is True

    def test_start_all_sse(self) -> None:
        cfg = MCPServerConfig(
            name="remote",
            url="http://localhost:9090/sse",
            transport="sse",
        )
        mgr = MCPManager([cfg])
        mgr.start_all()

        # SSE servers are marked alive optimistically
        assert mgr.is_alive("remote") is True

    def test_start_all_sse_no_url(self) -> None:
        cfg = MCPServerConfig(name="bad", transport="sse")
        mgr = MCPManager([cfg])
        mgr.start_all()
        assert mgr.is_alive("bad") is False

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_stop_all_terminates_processes(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="test", command=["echo"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        mgr.stop_all()

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called()
        assert mgr.is_alive("test") is False

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_stop_all_kills_on_timeout(self, mock_popen: MagicMock) -> None:
        import subprocess as sp

        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        # First wait (after terminate) times out, second wait (after kill) succeeds
        mock_proc.wait.side_effect = [
            sp.TimeoutExpired(cmd="echo", timeout=5),
            None,
        ]
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="stubborn", command=["echo"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        mgr.stop_all()

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    def test_stop_all_idempotent(self) -> None:
        cfg = MCPServerConfig(name="sse", url="http://x", transport="sse")
        mgr = MCPManager([cfg])
        mgr.start_all()
        mgr.stop_all()
        # Second call should not raise
        mgr.stop_all()

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_start_all_skips_already_started(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="test", command=["echo"])
        mgr = MCPManager([cfg])
        mgr.start_all()
        mgr.start_all()

        # Popen should only be called once
        assert mock_popen.call_count == 1

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_start_all_handles_popen_failure(self, mock_popen: MagicMock) -> None:
        mock_popen.side_effect = FileNotFoundError("npx not found")

        cfg = MCPServerConfig(name="missing", command=["npx", "-y", "pkg"])
        mgr = MCPManager([cfg])
        # Should not raise — failure is logged as warning
        mgr.start_all()
        assert mgr.is_alive("missing") is False

    def test_start_all_skips_empty_command(self) -> None:
        cfg = MCPServerConfig(name="empty", command=[])
        mgr = MCPManager([cfg])
        mgr.start_all()
        assert mgr.is_alive("empty") is False


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


class TestMCPManagerHealthChecks:
    """Tests for MCPManager.is_alive()."""

    def test_unknown_server_returns_false(self) -> None:
        mgr = MCPManager()
        assert mgr.is_alive("nonexistent") is False

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_detects_dead_process(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = 1  # Exited with code 1
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="dying", command=["echo"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        # poll() returns non-None -> dead
        assert mgr.is_alive("dying") is False

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_detects_alive_process(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None  # Still running
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="healthy", command=["echo"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        assert mgr.is_alive("healthy") is True


# ---------------------------------------------------------------------------
# get_server_info
# ---------------------------------------------------------------------------


class TestMCPManagerGetServerInfo:
    """Tests for MCPManager.get_server_info()."""

    def test_returns_config_for_known_server(self) -> None:
        cfg = MCPServerConfig(name="github", command=["npx"])
        mgr = MCPManager([cfg])
        info = mgr.get_server_info("github")
        assert info is not None
        assert info.name == "github"

    def test_returns_none_for_unknown(self) -> None:
        mgr = MCPManager()
        assert mgr.get_server_info("nope") is None


# ---------------------------------------------------------------------------
# build_mcp_config
# ---------------------------------------------------------------------------


class TestMCPManagerBuildConfig:
    """Tests for MCPManager.build_mcp_config()."""

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_builds_config_for_all_alive(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        configs = [
            MCPServerConfig(name="github", command=["npx", "-y", "gh-mcp"]),
            MCPServerConfig(name="remote", url="http://x/sse", transport="sse"),
        ]
        mgr = MCPManager(configs)
        mgr.start_all()

        result = mgr.build_mcp_config()
        assert result is not None
        assert "mcpServers" in result
        assert "github" in result["mcpServers"]
        assert "remote" in result["mcpServers"]

    def test_returns_none_when_no_servers_alive(self) -> None:
        cfg = MCPServerConfig(name="dead", command=["echo"])
        mgr = MCPManager([cfg])
        # Not started, so not alive
        result = mgr.build_mcp_config()
        assert result is None

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_builds_config_for_subset(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        configs = [
            MCPServerConfig(name="github", command=["npx"]),
            MCPServerConfig(name="filesystem", command=["node"]),
        ]
        mgr = MCPManager(configs)
        mgr.start_all()

        result = mgr.build_mcp_config(server_names=["github"])
        assert result is not None
        assert "github" in result["mcpServers"]
        assert "filesystem" not in result["mcpServers"]

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_excludes_dead_servers(self, mock_popen: MagicMock) -> None:
        alive_proc = MagicMock()
        alive_proc.pid = 100
        alive_proc.poll.return_value = None

        dead_proc = MagicMock()
        dead_proc.pid = 200
        dead_proc.poll.return_value = 1

        mock_popen.side_effect = [alive_proc, dead_proc]

        configs = [
            MCPServerConfig(name="alive", command=["echo"]),
            MCPServerConfig(name="dead", command=["fail"]),
        ]
        mgr = MCPManager(configs)
        mgr.start_all()

        result = mgr.build_mcp_config()
        assert result is not None
        assert "alive" in result["mcpServers"]
        assert "dead" not in result["mcpServers"]


# ---------------------------------------------------------------------------
# build_mcp_config_for_task
# ---------------------------------------------------------------------------


class TestBuildMCPConfigForTask:
    """Tests for MCPManager.build_mcp_config_for_task()."""

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_merges_task_with_base(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="github", command=["npx"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        base = {"mcpServers": {"tavily": {"command": "npx", "args": ["tavily"]}}}
        result = mgr.build_mcp_config_for_task(
            task_mcp_servers=["github"],
            base_config=base,
        )
        assert result is not None
        assert "tavily" in result["mcpServers"]
        assert "github" in result["mcpServers"]

    def test_returns_none_when_both_empty(self) -> None:
        mgr = MCPManager()
        result = mgr.build_mcp_config_for_task(
            task_mcp_servers=None,
            base_config=None,
        )
        assert result is None

    def test_returns_base_when_no_task_servers(self) -> None:
        mgr = MCPManager()
        base = {"mcpServers": {"tavily": {"command": "npx"}}}
        result = mgr.build_mcp_config_for_task(
            task_mcp_servers=None,
            base_config=base,
        )
        assert result == base

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_returns_task_when_no_base(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="github", command=["npx"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        result = mgr.build_mcp_config_for_task(
            task_mcp_servers=["github"],
            base_config=None,
        )
        assert result is not None
        assert "github" in result["mcpServers"]

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_task_overrides_base_on_conflict(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="github", command=["custom-github"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        base = {"mcpServers": {"github": {"command": "npx", "args": ["old"]}}}
        result = mgr.build_mcp_config_for_task(
            task_mcp_servers=["github"],
            base_config=base,
        )
        assert result is not None
        # Task config should win
        assert result["mcpServers"]["github"]["command"] == "custom-github"


# ---------------------------------------------------------------------------
# Env merging
# ---------------------------------------------------------------------------


class TestEnvMerge:
    """Tests for environment variable merging in server startup."""

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_merges_env_with_current(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(
            name="test",
            command=["echo"],
            env={"CUSTOM_VAR": "custom_value"},
        )
        mgr = MCPManager([cfg])
        mgr.start_all()

        call_kwargs = mock_popen.call_args.kwargs
        assert call_kwargs["env"] is not None
        assert call_kwargs["env"]["CUSTOM_VAR"] == "custom_value"
        # Should also have inherited env vars (PATH at minimum)
        assert "PATH" in call_kwargs["env"]

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_no_extra_env_passes_none(self, mock_popen: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="test", command=["echo"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        call_kwargs = mock_popen.call_args.kwargs
        assert call_kwargs["env"] is None


# ---------------------------------------------------------------------------
# Integration with spawner (mock adapter)
# ---------------------------------------------------------------------------


class TestSpawnerMCPManagerIntegration:
    """Tests that MCPManager integrates correctly with AgentSpawner."""

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_spawner_uses_mcp_manager_for_task_servers(
        self,
        mock_popen: MagicMock,
        tmp_path: Path,
        make_task: object,
        mock_adapter_factory: object,
    ) -> None:
        from bernstein.core.spawner import AgentSpawner

        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        cfg = MCPServerConfig(name="github", command=["npx", "-y", "gh-mcp"])
        mgr = MCPManager([cfg])
        mgr.start_all()

        adapter = mock_adapter_factory()  # type: ignore[operator]
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            mcp_manager=mgr,
        )

        task = make_task(mcp_servers=["github"])  # type: ignore[operator]
        spawner.spawn_for_tasks([task])

        call_kwargs = adapter.spawn.call_args.kwargs
        assert call_kwargs["mcp_config"] is not None
        assert "github" in call_kwargs["mcp_config"]["mcpServers"]

    @patch("bernstein.core.mcp_manager.subprocess.Popen")
    def test_spawner_all_servers_when_task_has_none(
        self,
        mock_popen: MagicMock,
        tmp_path: Path,
        make_task: object,
        mock_adapter_factory: object,
    ) -> None:
        from bernstein.core.spawner import AgentSpawner

        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        configs = [
            MCPServerConfig(name="github", command=["npx"]),
            MCPServerConfig(name="filesystem", command=["node"]),
        ]
        mgr = MCPManager(configs)
        mgr.start_all()

        adapter = mock_adapter_factory()  # type: ignore[operator]
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            mcp_manager=mgr,
        )

        task = make_task()  # type: ignore[operator]
        spawner.spawn_for_tasks([task])

        call_kwargs = adapter.spawn.call_args.kwargs
        assert call_kwargs["mcp_config"] is not None
        servers = call_kwargs["mcp_config"]["mcpServers"]
        assert "github" in servers
        assert "filesystem" in servers
