"""MCP skill discovery parity tests (issue #3077)."""

from __future__ import annotations

import json
import re

import pytest

from bernstein.core.skills.index_builder import SKILL_INDEX_RESOURCE_URI
from bernstein.mcp.server import create_mcp_server

_INDEX_SIZE_CEILING = 16 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@pytest.mark.asyncio
async def test_no_arg_tool_and_resource_return_the_same_compact_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "0")
    mcp = create_mcp_server(tier="all")

    tool_result = await mcp.call_tool("load_skill", {})
    tool_body = tool_result[0][0].text  # type: ignore[index]
    resource_contents = await mcp.read_resource(SKILL_INDEX_RESOURCE_URI)
    resource_body = next(iter(resource_contents)).content

    assert tool_body == resource_body
    assert len(tool_body.encode("utf-8")) < _INDEX_SIZE_CEILING

    index = json.loads(tool_body)
    assert index["skills"]
    for entry in index["skills"]:
        assert set(entry) == {"content_hash", "description", "name"}
        assert "\n" not in entry["description"]
        assert _SHA256_RE.fullmatch(entry["content_hash"])
        assert "body" not in entry


@pytest.mark.asyncio
async def test_named_load_skill_keeps_returning_the_full_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "0")
    mcp = create_mcp_server(tier="all")
    index_result = await mcp.call_tool("load_skill", {})
    index = json.loads(index_result[0][0].text)  # type: ignore[index]
    name = index["skills"][0]["name"]

    named_result = await mcp.call_tool("load_skill", {"name": name})
    loaded = json.loads(named_result[0][0].text)  # type: ignore[index]

    assert loaded["name"] == name
    assert loaded["body"]
