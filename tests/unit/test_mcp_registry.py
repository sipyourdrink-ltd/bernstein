"""Tests for MCP server auto-discovery and per-task configuration."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from bernstein.core.mcp_registry import MCPRegistry, MCPServerEntry

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# MCPServerEntry
# ---------------------------------------------------------------------------


class TestMCPServerEntry:
    """Tests for MCPServerEntry dataclass."""

    def test_env_available_when_all_set(self) -> None:
        entry = MCPServerEntry(name="test", package="pkg", env_required=("KEY_A", "KEY_B"))
        with patch.dict(os.environ, {"KEY_A": "a", "KEY_B": "b"}):
            assert entry.env_available() is True

    def test_env_available_when_missing(self) -> None:
        entry = MCPServerEntry(name="test", package="pkg", env_required=("KEY_A",))
        with patch.dict(os.environ, {}, clear=True):
            assert entry.env_available() is False

    def test_env_available_when_no_requirements(self) -> None:
        entry = MCPServerEntry(name="test", package="pkg", env_required=())
        assert entry.env_available() is True

    def test_to_mcp_config_default_args(self) -> None:
        entry = MCPServerEntry(name="tavily", package="@anthropic/tavily-mcp")
        config = entry.to_mcp_config()
        assert config["command"] == "npx"
        assert config["args"] == ["-y", "@anthropic/tavily-mcp"]
        assert "env" not in config

    def test_to_mcp_config_custom_args(self) -> None:
        entry = MCPServerEntry(name="custom", package="pkg", command="node", args=("server.js", "--port", "3000"))
        config = entry.to_mcp_config()
        assert config["command"] == "node"
        assert config["args"] == ["server.js", "--port", "3000"]

    def test_to_mcp_config_includes_env_vars(self) -> None:
        entry = MCPServerEntry(name="tavily", package="pkg", env_required=("TAVILY_API_KEY",))
        with patch.dict(os.environ, {"TAVILY_API_KEY": "secret123"}):
            config = entry.to_mcp_config()
        assert config["env"] == {"TAVILY_API_KEY": "secret123"}


# ---------------------------------------------------------------------------
# MCPRegistry loading
# ---------------------------------------------------------------------------


class TestMCPRegistryLoading:
    """Tests for MCPRegistry YAML loading."""

    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        catalog = tmp_path / "mcp_servers.yaml"
        catalog.write_text(
            "servers:\n"
            "  - name: tavily\n"
            '    package: "@anthropic/tavily-mcp"\n'
            "    capabilities: [web_search]\n"
            '    keywords: ["search the web"]\n'
            "    env_required: [TAVILY_API_KEY]\n"
        )
        registry = MCPRegistry(config_path=catalog)
        assert len(registry.servers) == 1
        assert registry.servers[0].name == "tavily"
        assert registry.servers[0].capabilities == ("web_search",)
        assert registry.servers[0].keywords == ("search the web",)

    def test_empty_when_file_missing(self, tmp_path: Path) -> None:
        registry = MCPRegistry(config_path=tmp_path / "missing.yaml")
        assert registry.servers == []

    def test_empty_when_no_path(self) -> None:
        registry = MCPRegistry(config_path=None)
        assert registry.servers == []

    def test_handles_malformed_yaml(self, tmp_path: Path) -> None:
        catalog = tmp_path / "mcp_servers.yaml"
        catalog.write_text("not: [valid yaml structure for servers")
        registry = MCPRegistry(config_path=catalog)
        assert registry.servers == []

    def test_skips_entries_without_name(self, tmp_path: Path) -> None:
        catalog = tmp_path / "mcp_servers.yaml"
        catalog.write_text("servers:\n  - package: pkg1\n  - name: valid\n    package: pkg2\n")
        registry = MCPRegistry(config_path=catalog)
        assert len(registry.servers) == 1
        assert registry.servers[0].name == "valid"

    def test_loads_custom_command_and_args(self, tmp_path: Path) -> None:
        catalog = tmp_path / "mcp_servers.yaml"
        catalog.write_text(
            "servers:\n"
            "  - name: custom\n"
            "    package: my-pkg\n"
            "    command: node\n"
            '    args: ["server.js", "--port", "3000"]\n'
        )
        registry = MCPRegistry(config_path=catalog)
        entry = registry.servers[0]
        assert entry.command == "node"
        assert entry.args == ("server.js", "--port", "3000")


# ---------------------------------------------------------------------------
# MCPRegistry detection
# ---------------------------------------------------------------------------


def _make_registry(servers: list[dict[str, Any]]) -> MCPRegistry:
    """Build a registry from raw server dicts without touching the filesystem."""
    registry = MCPRegistry(config_path=None)
    for raw in servers:
        entry = MCPServerEntry(
            name=raw["name"],
            package=raw["package"],
            capabilities=tuple(raw.get("capabilities", [])),
            keywords=tuple(raw.get("keywords", [])),
            env_required=tuple(raw.get("env_required", [])),
        )
        registry._servers.append(entry)
        import re

        for kw in entry.keywords:
            pattern = re.compile(re.escape(kw), re.IGNORECASE)
            registry._keyword_patterns.append((pattern, entry))
    return registry


class TestMCPRegistryDetection:
    """Tests for keyword-based server detection."""

    def test_detects_by_keyword(self) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg", "keywords": ["search the web"]},
            ]
        )
        result = registry.detect_servers("Please search the web for Python docs")
        assert len(result) == 1
        assert result[0].name == "tavily"

    def test_keyword_case_insensitive(self) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg", "keywords": ["web search"]},
            ]
        )
        result = registry.detect_servers("Use WEB SEARCH to find the answer")
        assert len(result) == 1

    def test_no_match_returns_empty(self) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg", "keywords": ["web search"]},
            ]
        )
        result = registry.detect_servers("Fix the CSS styling bug")
        assert result == []

    def test_deduplicates_matches(self) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg", "keywords": ["search the web", "web search"]},
            ]
        )
        result = registry.detect_servers("Search the web using web search")
        assert len(result) == 1

    def test_detects_by_file_extension(self) -> None:
        registry = _make_registry(
            [
                {"name": "postgres", "package": "pkg", "keywords": [".sql"]},
            ]
        )
        result = registry.detect_servers("Run migration", owned_files=["migrations/001.sql"])
        assert len(result) == 1
        assert result[0].name == "postgres"

    def test_detects_by_capability(self) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg", "capabilities": ["web_search"]},
            ]
        )
        result = registry.detect_servers("Do the thing", requested_capabilities=["web_search"])
        assert len(result) == 1
        assert result[0].name == "tavily"

    def test_multiple_servers_detected(self) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg1", "keywords": ["web search"]},
                {"name": "github", "package": "pkg2", "keywords": ["create pr"]},
                {"name": "slack", "package": "pkg3", "keywords": ["slack message"]},
            ]
        )
        result = registry.detect_servers("Web search for docs, then create PR")
        names = {s.name for s in result}
        assert names == {"tavily", "github"}


# ---------------------------------------------------------------------------
# MCPRegistry filtering and config building
# ---------------------------------------------------------------------------


class TestMCPRegistryFiltering:
    """Tests for env var filtering."""

    def test_filter_keeps_available(self) -> None:
        entry = MCPServerEntry(name="t", package="p", env_required=("KEY",))
        registry = MCPRegistry(config_path=None)
        with patch.dict(os.environ, {"KEY": "val"}):
            result = registry.filter_available([entry])
        assert len(result) == 1

    def test_filter_removes_unavailable(self) -> None:
        entry = MCPServerEntry(name="t", package="p", env_required=("MISSING_KEY",))
        registry = MCPRegistry(config_path=None)
        with patch.dict(os.environ, {}, clear=True):
            result = registry.filter_available([entry])
        assert result == []

    def test_filter_keeps_no_env_required(self) -> None:
        entry = MCPServerEntry(name="t", package="p", env_required=())
        registry = MCPRegistry(config_path=None)
        result = registry.filter_available([entry])
        assert len(result) == 1


class TestMCPRegistryConfigBuilding:
    """Tests for MCP config dict construction."""

    def test_build_mcp_config(self) -> None:
        entry = MCPServerEntry(name="tavily", package="@anthropic/tavily-mcp")
        registry = MCPRegistry(config_path=None)
        config = registry.build_mcp_config([entry])
        assert config is not None
        assert "mcpServers" in config
        assert "tavily" in config["mcpServers"]
        assert config["mcpServers"]["tavily"]["args"] == ["-y", "@anthropic/tavily-mcp"]

    def test_build_mcp_config_empty_returns_none(self) -> None:
        registry = MCPRegistry(config_path=None)
        assert registry.build_mcp_config([]) is None


# ---------------------------------------------------------------------------
# MCPRegistry resolve_for_tasks
# ---------------------------------------------------------------------------


class TestMCPRegistryResolveForTasks:
    """Tests for the end-to-end resolve_for_tasks method."""

    def test_merges_auto_with_base(self, make_task) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg", "keywords": ["web search"], "env_required": []},
            ]
        )
        base_config: dict[str, Any] = {
            "mcpServers": {"existing": {"command": "npx", "args": ["-y", "existing-pkg"]}},
        }
        task = make_task(description="Use web search to find docs")

        result = registry.resolve_for_tasks([task], base_config=base_config)

        assert result is not None
        assert "existing" in result["mcpServers"]
        assert "tavily" in result["mcpServers"]

    def test_base_config_wins_on_conflict(self, make_task) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "auto-pkg", "keywords": ["web search"], "env_required": []},
            ]
        )
        base_config: dict[str, Any] = {
            "mcpServers": {"tavily": {"command": "custom", "args": ["--custom"]}},
        }
        task = make_task(description="Use web search")

        result = registry.resolve_for_tasks([task], base_config=base_config)

        assert result is not None
        # Base config should win
        assert result["mcpServers"]["tavily"]["command"] == "custom"

    def test_no_servers_returns_base(self, make_task) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg", "keywords": ["web search"], "env_required": []},
            ]
        )
        base_config: dict[str, Any] = {
            "mcpServers": {"existing": {"command": "npx"}},
        }
        task = make_task(description="Fix CSS bug")

        result = registry.resolve_for_tasks([task], base_config=base_config)

        assert result == base_config

    def test_no_servers_no_base_returns_none(self, make_task) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg", "keywords": ["web search"], "env_required": []},
            ]
        )
        task = make_task(description="Fix CSS bug")

        result = registry.resolve_for_tasks([task], base_config=None)
        assert result is None

    def test_skips_servers_with_missing_env(self, make_task) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg", "keywords": ["web search"], "env_required": ["TAVILY_API_KEY"]},
            ]
        )
        task = make_task(description="Use web search to find docs")

        with patch.dict(os.environ, {}, clear=True):
            result = registry.resolve_for_tasks([task], base_config=None)

        assert result is None

    def test_combines_multiple_task_descriptions(self, make_task) -> None:
        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg1", "keywords": ["web search"], "env_required": []},
                {"name": "github", "package": "pkg2", "keywords": ["create pr"], "env_required": []},
            ]
        )
        task1 = make_task(id="T-001", description="Web search for examples")
        task2 = make_task(id="T-002", description="Then create PR with results")

        result = registry.resolve_for_tasks([task1, task2], base_config=None)

        assert result is not None
        assert "tavily" in result["mcpServers"]
        assert "github" in result["mcpServers"]


# ---------------------------------------------------------------------------
# Spawner integration with MCPRegistry
# ---------------------------------------------------------------------------


class TestSpawnerMCPRegistryIntegration:
    """Tests that MCPRegistry integrates correctly with AgentSpawner."""

    def test_spawner_uses_registry_for_per_task_config(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        from bernstein.core.spawner import AgentSpawner

        adapter = mock_adapter_factory()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        registry = _make_registry(
            [
                {"name": "tavily", "package": "pkg", "keywords": ["web search"], "env_required": []},
            ]
        )
        base_config: dict[str, Any] = {
            "mcpServers": {"base": {"command": "npx"}},
        }

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            mcp_config=base_config,
            mcp_registry=registry,
        )
        spawner.spawn_for_tasks([make_task(description="Use web search")])

        call_kwargs = adapter.spawn.call_args.kwargs
        mcp = call_kwargs["mcp_config"]
        assert mcp is not None
        assert "base" in mcp["mcpServers"]
        assert "tavily" in mcp["mcpServers"]

    def test_spawner_without_registry_uses_base_config(self, tmp_path: Path, make_task, mock_adapter_factory) -> None:
        from bernstein.core.spawner import AgentSpawner

        adapter = mock_adapter_factory()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        base_config: dict[str, Any] = {
            "mcpServers": {"base": {"command": "npx"}},
        }

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            mcp_config=base_config,
            mcp_registry=None,
        )
        spawner.spawn_for_tasks([make_task()])

        call_kwargs = adapter.spawn.call_args.kwargs
        assert call_kwargs["mcp_config"] == base_config
