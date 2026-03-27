"""Tests for MCP config loading, merging, and passing to spawned agents."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.claude import ClaudeCodeAdapter, _resolve_env_vars, load_mcp_config
from bernstein.core.models import ModelConfig
from bernstein.core.seed import parse_seed
from bernstein.core.spawner import AgentSpawner

# ---------------------------------------------------------------------------
# load_mcp_config
# ---------------------------------------------------------------------------


class TestLoadMcpConfig:
    """Tests for load_mcp_config merging logic."""

    def test_returns_none_when_no_sources(self, tmp_path: Path) -> None:
        with patch("bernstein.adapters.claude.Path.home", return_value=tmp_path):
            result = load_mcp_config()
        assert result is None

    def test_reads_global_mcp_json(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        mcp_json = {
            "mcpServers": {
                "tavily": {"command": "npx", "args": ["-y", "tavily-mcp"]},
            }
        }
        (claude_dir / "mcp.json").write_text(json.dumps(mcp_json))

        with patch("bernstein.adapters.claude.Path.home", return_value=tmp_path):
            result = load_mcp_config()

        assert result is not None
        assert "mcpServers" in result
        assert "tavily" in result["mcpServers"]
        assert result["mcpServers"]["tavily"]["command"] == "npx"

    def test_project_servers_override_global(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        mcp_json = {
            "mcpServers": {
                "tavily": {"command": "npx", "args": ["old"]},
            }
        }
        (claude_dir / "mcp.json").write_text(json.dumps(mcp_json))

        project_servers = {
            "tavily": {"command": "node", "args": ["new"]},
        }

        with patch("bernstein.adapters.claude.Path.home", return_value=tmp_path):
            result = load_mcp_config(project_servers=project_servers)

        assert result is not None
        assert result["mcpServers"]["tavily"]["command"] == "node"
        assert result["mcpServers"]["tavily"]["args"] == ["new"]

    def test_merges_global_and_project_servers(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        mcp_json = {
            "mcpServers": {
                "tavily": {"command": "npx", "args": ["tavily"]},
            }
        }
        (claude_dir / "mcp.json").write_text(json.dumps(mcp_json))

        project_servers = {
            "browser": {"command": "npx", "args": ["browser-mcp"]},
        }

        with patch("bernstein.adapters.claude.Path.home", return_value=tmp_path):
            result = load_mcp_config(project_servers=project_servers)

        assert result is not None
        assert "tavily" in result["mcpServers"]
        assert "browser" in result["mcpServers"]

    def test_project_only_no_global(self, tmp_path: Path) -> None:
        project_servers = {
            "custom": {"command": "my-tool", "args": []},
        }

        with patch("bernstein.adapters.claude.Path.home", return_value=tmp_path):
            result = load_mcp_config(project_servers=project_servers)

        assert result is not None
        assert "custom" in result["mcpServers"]

    def test_handles_malformed_global_json(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "mcp.json").write_text("not valid json {{{")

        result_no_project = load_mcp_config(project_servers=None)
        # Should not crash, returns None since no servers found
        # (global parse failed, no project servers)

    def test_handles_global_json_without_mcpservers_key(self, tmp_path: Path) -> None:
        """If mcp.json is a flat dict of servers (no mcpServers wrapper)."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        flat = {"tavily": {"command": "npx", "args": ["t"]}}
        (claude_dir / "mcp.json").write_text(json.dumps(flat))

        with patch("bernstein.adapters.claude.Path.home", return_value=tmp_path):
            result = load_mcp_config()

        assert result is not None
        assert "tavily" in result["mcpServers"]


# ---------------------------------------------------------------------------
# _resolve_env_vars
# ---------------------------------------------------------------------------


class TestResolveEnvVars:
    """Tests for environment variable resolution in MCP config."""

    def test_resolves_env_var_string(self) -> None:
        with patch.dict(os.environ, {"MY_KEY": "secret123"}):
            assert _resolve_env_vars("${MY_KEY}") == "secret123"

    def test_leaves_missing_env_var_as_is(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_env_vars("${NONEXISTENT}") == "${NONEXISTENT}"

    def test_resolves_nested_dict(self) -> None:
        with patch.dict(os.environ, {"API_KEY": "abc"}):
            obj = {"env": {"key": "${API_KEY}"}, "name": "test"}
            result = _resolve_env_vars(obj)
            assert result == {"env": {"key": "abc"}, "name": "test"}

    def test_resolves_list_items(self) -> None:
        with patch.dict(os.environ, {"VAL": "x"}):
            result = _resolve_env_vars(["${VAL}", "plain"])
            assert result == ["x", "plain"]

    def test_leaves_non_env_strings_alone(self) -> None:
        assert _resolve_env_vars("plain text") == "plain text"
        assert _resolve_env_vars(42) == 42
        assert _resolve_env_vars(True) is True


# ---------------------------------------------------------------------------
# Seed parsing of mcp_servers
# ---------------------------------------------------------------------------


class TestSeedMcpServers:
    """Tests for mcp_servers in bernstein.yaml parsing."""

    def test_parses_mcp_servers_from_yaml(self, tmp_path: Path) -> None:
        yaml_content = """\
goal: "Test project"
mcp_servers:
  tavily:
    command: npx
    args: ["-y", "@anthropic/tavily-mcp"]
    env:
      TAVILY_API_KEY: "${TAVILY_API_KEY}"
"""
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text(yaml_content)
        cfg = parse_seed(seed_file)

        assert cfg.mcp_servers is not None
        assert "tavily" in cfg.mcp_servers
        assert cfg.mcp_servers["tavily"]["command"] == "npx"

    def test_mcp_servers_default_is_none(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text('goal: "Test"\n')
        cfg = parse_seed(seed_file)
        assert cfg.mcp_servers is None

    def test_invalid_mcp_servers_type_raises(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text('goal: "Test"\nmcp_servers: "not a dict"\n')
        with pytest.raises(Exception, match="mcp_servers must be a mapping"):
            parse_seed(seed_file)


# ---------------------------------------------------------------------------
# Spawner passes MCP config to adapter
# ---------------------------------------------------------------------------


class TestSpawnerMcpPassthrough:
    """Tests that MCP config flows from spawner to adapter."""

    def test_passes_mcp_config_to_adapter(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        mcp_config = {"mcpServers": {"tavily": {"command": "npx"}}}

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, mcp_config=mcp_config)
        spawner.spawn_for_tasks([make_task()])

        call_kwargs = adapter.spawn.call_args.kwargs
        assert call_kwargs["mcp_config"] == mcp_config

    def test_passes_none_when_no_mcp_config(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        adapter = mock_adapter_factory()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        spawner = AgentSpawner(adapter, templates_dir, tmp_path, mcp_config=None)
        spawner.spawn_for_tasks([make_task()])

        call_kwargs = adapter.spawn.call_args.kwargs
        assert call_kwargs["mcp_config"] is None


# ---------------------------------------------------------------------------
# Claude adapter --mcp-config flag
# ---------------------------------------------------------------------------


class TestClaudeAdapterMcpFlag:
    """Tests that ClaudeCodeAdapter builds correct CLI command with MCP."""

    def test_includes_mcp_config_in_command(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()
        mcp = {"mcpServers": {"tavily": {"command": "npx"}}}

        with patch("bernstein.adapters.claude.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 999
            mock_popen.return_value = mock_proc

            adapter.spawn(
                prompt="test prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="normal"),
                session_id="test-001",
                mcp_config=mcp,
            )

            # First Popen call is the claude command; second is the wrapper script
            cmd = mock_popen.call_args_list[0].args[0]
            assert "--mcp-config" in cmd
            mcp_idx = cmd.index("--mcp-config")
            mcp_json = json.loads(cmd[mcp_idx + 1])
            assert "mcpServers" in mcp_json
            assert "tavily" in mcp_json["mcpServers"]

    def test_no_mcp_flag_when_none(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()

        with patch("bernstein.adapters.claude.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 999
            mock_popen.return_value = mock_proc

            adapter.spawn(
                prompt="test prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="normal"),
                session_id="test-002",
                mcp_config=None,
            )

            cmd = mock_popen.call_args_list[0].args[0]
            assert "--mcp-config" not in cmd
