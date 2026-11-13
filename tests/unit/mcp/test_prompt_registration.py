"""Read the registered MCP prompts through an in-process server."""

import pytest
from mcp.server.fastmcp import FastMCP

from bernstein.mcp.prompts import register_prompt_resources


@pytest.mark.asyncio
async def test_registered_prompt_names_and_rendering() -> None:
    server = FastMCP("test")
    register_prompt_resources(server)

    prompts = await server.list_prompts()
    assert {prompt.name for prompt in prompts} == {
        "orchestrate_goal",
        "triage_failed_tasks",
        "cost_recap",
    }
    result = await server.get_prompt("orchestrate_goal", {"goal": "Render a sample report", "scope": "small"})
    (message,) = result.messages
    assert message.content.type == "text"
    assert "Goal: Render a sample report" in message.content.text
    assert "Suggested role: backend" in message.content.text
    assert "Scope: small" in message.content.text
