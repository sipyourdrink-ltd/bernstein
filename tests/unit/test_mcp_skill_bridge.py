"""Tests for bernstein.core.mcp_skill_bridge — MCP-to-skill bridge."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from bernstein.core.mcp_skill_bridge import (
    _BUILDERS,
    MCPToolInfo,
    build_skills_from_mcp_server,
    collect_mcp_skills,
    register_skill_builder,
)
from bernstein.core.skill_discovery import SkillSource
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_builders() -> Generator[None, None, None]:
    """Clear the global _BUILDERS registry before and after each test."""
    _BUILDERS.clear()
    yield
    _BUILDERS.clear()


# ---------------------------------------------------------------------------
# MCPToolInfo
# ---------------------------------------------------------------------------


class TestMCPToolInfo:
    def test_fields_accessible(self) -> None:
        info = MCPToolInfo(name="my_tool", description="Does stuff", server_name="myserver")
        assert info.name == "my_tool"
        assert info.description == "Does stuff"
        assert info.server_name == "myserver"

    def test_frozen(self) -> None:
        info = MCPToolInfo(name="t", description="d", server_name="s")
        with pytest.raises((AttributeError, TypeError)):
            info.name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# register_skill_builder (write-once)
# ---------------------------------------------------------------------------


class TestRegisterSkillBuilder:
    def test_registers_builder(self) -> None:
        register_skill_builder("srv1", lambda: [])
        assert "srv1" in _BUILDERS

    def test_write_once_second_registration_ignored(self) -> None:
        original: list[MCPToolInfo] = [MCPToolInfo("t1", "desc1", "srv")]
        second: list[MCPToolInfo] = [MCPToolInfo("t2", "desc2", "srv")]

        register_skill_builder("srv", lambda: original)
        register_skill_builder("srv", lambda: second)  # should be ignored

        # Only the first builder is in the registry
        tools = _BUILDERS["srv"]()
        assert len(tools) == 1
        assert tools[0].name == "t1"

    def test_multiple_different_servers_registered(self) -> None:
        register_skill_builder("srv_a", lambda: [])
        register_skill_builder("srv_b", lambda: [])
        assert "srv_a" in _BUILDERS
        assert "srv_b" in _BUILDERS


# ---------------------------------------------------------------------------
# collect_mcp_skills
# ---------------------------------------------------------------------------


class TestCollectMcpSkills:
    def test_returns_empty_when_no_builders_registered(self) -> None:
        skills = collect_mcp_skills()
        assert skills == {}

    def test_calls_all_registered_builders(self) -> None:
        calls: list[str] = []

        def builder_a() -> list[MCPToolInfo]:
            calls.append("a")
            return [MCPToolInfo("tool_a", "desc a", "srv_a")]

        def builder_b() -> list[MCPToolInfo]:
            calls.append("b")
            return [MCPToolInfo("tool_b", "desc b", "srv_b")]

        register_skill_builder("srv_a", builder_a)
        register_skill_builder("srv_b", builder_b)

        skills = collect_mcp_skills()

        assert sorted(calls) == ["a", "b"]
        assert "tool_a" in skills
        assert "tool_b" in skills

    def test_collected_skills_have_mcp_source(self) -> None:
        register_skill_builder(
            "my_srv",
            lambda: [MCPToolInfo("my_tool", "does things", "my_srv")],
        )
        skills = collect_mcp_skills()
        assert skills["my_tool"].source == SkillSource.MCP

    def test_skill_name_and_description_are_preserved(self) -> None:
        register_skill_builder(
            "srv",
            lambda: [MCPToolInfo("search", "Search the web", "srv")],
        )
        skills = collect_mcp_skills()
        skill = skills["search"]
        assert skill.name == "search"
        assert skill.description == "Search the web"

    def test_skill_origin_encodes_server_and_tool(self) -> None:
        register_skill_builder(
            "my_srv",
            lambda: [MCPToolInfo("my_tool", "does things", "my_srv")],
        )
        skills = collect_mcp_skills()
        assert skills["my_tool"].origin == "mcp://my_srv/my_tool"

    def test_faulty_builder_is_skipped(self) -> None:
        def bad_builder() -> list[MCPToolInfo]:
            raise RuntimeError("builder is broken")

        register_skill_builder("bad_srv", bad_builder)
        register_skill_builder("good_srv", lambda: [MCPToolInfo("ok_tool", "fine", "good_srv")])

        skills = collect_mcp_skills()
        assert "ok_tool" in skills
        assert len(skills) == 1  # bad server produced no skills


# ---------------------------------------------------------------------------
# build_skills_from_mcp_server
# ---------------------------------------------------------------------------


class TestBuildSkillsFromMcpServer:
    def _make_server(self) -> FastMCP:  # type: ignore[type-arg]
        mcp: FastMCP = FastMCP("test_srv")  # type: ignore[type-arg]

        @mcp.tool()
        def do_something(x: int) -> str:
            """Perform an important action."""
            return str(x)

        @mcp.tool()
        def do_another(y: str) -> str:
            """Perform another action."""
            return y

        return mcp

    def test_extracts_all_tool_names(self) -> None:
        mcp = self._make_server()
        infos = build_skills_from_mcp_server(mcp)
        names = {i.name for i in infos}
        assert "do_something" in names
        assert "do_another" in names

    def test_extracts_tool_descriptions(self) -> None:
        mcp = self._make_server()
        infos = build_skills_from_mcp_server(mcp)
        by_name = {i.name: i for i in infos}
        assert by_name["do_something"].description == "Perform an important action."

    def test_server_name_is_set_on_each_info(self) -> None:
        mcp = self._make_server()
        infos = build_skills_from_mcp_server(mcp)
        assert all(i.server_name == "test_srv" for i in infos)

    def test_empty_server_returns_empty_list(self) -> None:
        mcp: FastMCP = FastMCP("empty_srv")  # type: ignore[type-arg]
        infos = build_skills_from_mcp_server(mcp)
        assert infos == []

    def test_integration_register_and_collect(self) -> None:
        """Full integration: register FastMCP builder then collect skills."""
        mcp: FastMCP = FastMCP("int_srv")  # type: ignore[type-arg]

        @mcp.tool()
        def ping() -> str:
            """Ping the server."""
            return "pong"

        register_skill_builder("int_srv", lambda: build_skills_from_mcp_server(mcp))
        skills = collect_mcp_skills()

        assert "ping" in skills
        assert skills["ping"].source == SkillSource.MCP
        assert skills["ping"].description == "Ping the server."
