"""Tests for the built-in MCP prompt catalogue (issue #1674).

Covers:

  * the FastMCP server registers the prompts and exposes ``prompts/list`` /
    ``prompts/get``;
  * each prompt renders deterministically from its arguments;
  * the streamable HTTP transport answers ``prompts/list`` and ``prompts/get``
    with the same catalogue.
"""

from __future__ import annotations

import asyncio
import json

import pytest

# ---------------------------------------------------------------------------
# Pure template rendering
# ---------------------------------------------------------------------------


def test_orchestrate_goal_template_includes_goal_role_scope() -> None:
    from bernstein.mcp.prompts import _orchestrate_goal_template

    body = _orchestrate_goal_template(goal="ship feature", role="backend", scope="small")
    assert "ship feature" in body
    assert "backend" in body
    assert "small" in body
    # Mentions the right Bernstein tools so a host preview is useful.
    assert "bernstein_run" in body
    assert "bernstein_run_status" in body


def test_triage_failed_tasks_template_uses_limit() -> None:
    from bernstein.mcp.prompts import _triage_failed_tasks_template

    body = _triage_failed_tasks_template(limit=3)
    assert "3" in body
    assert "failed" in body


def test_cost_recap_template_uses_window() -> None:
    from bernstein.mcp.prompts import _cost_recap_template

    body = _cost_recap_template(window="last 7 days")
    assert "last 7 days" in body
    assert "bernstein_status" in body


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_MCP_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_TOOL_TIER", raising=False)
    monkeypatch.delenv("BERNSTEIN_MCP_COST_METER", raising=False)


def test_fastmcp_server_lists_prompts(_clean_env: None) -> None:
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(tier="standard")
    prompts = asyncio.run(mcp.list_prompts())
    names = {p.name for p in prompts}
    assert {"orchestrate_goal", "triage_failed_tasks", "cost_recap"} <= names


def test_fastmcp_server_renders_orchestrate_goal(_clean_env: None) -> None:
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(tier="standard")
    rendered = asyncio.run(mcp.get_prompt("orchestrate_goal", {"goal": "fix flaky test", "role": "qa"}))
    assert rendered.messages, "prompt should render at least one message"
    msg = rendered.messages[0]
    assert msg.role == "user"
    assert msg.content.type == "text"
    assert "fix flaky test" in msg.content.text
    assert "qa" in msg.content.text


def test_capability_card_advertises_prompt_catalogue(_clean_env: None) -> None:
    from bernstein.mcp.capability import build_capability_card

    card = build_capability_card()
    assert card["prompts"]["supported"] is True
    assert "orchestrate_goal" in card["prompts"]["catalogue"]
    assert "triage_failed_tasks" in card["prompts"]["catalogue"]
    assert "cost_recap" in card["prompts"]["catalogue"]


# ---------------------------------------------------------------------------
# Streamable HTTP transport
# ---------------------------------------------------------------------------


def _post_jsonrpc(transport, method: str, params: dict | None = None, req_id: int = 1) -> dict:
    """Send a JSON-RPC POST through the transport and return the decoded result."""
    payload = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload).encode()
    status, _, resp_body = asyncio.run(transport.handle_request("POST", "/mcp", {}, body))
    assert status == 200, f"got {status}: {resp_body!r}"
    return json.loads(resp_body)


def test_http_transport_lists_prompts(_clean_env: None) -> None:
    from bernstein.mcp.remote_transport import (
        RemoteMCPConfig,
        StreamableHTTPTransport,
    )

    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg)
    resp = _post_jsonrpc(transport, "prompts/list")
    prompts = resp["result"]["prompts"]
    names = {p["name"] for p in prompts}
    assert {"orchestrate_goal", "triage_failed_tasks", "cost_recap"} <= names
    # Arguments are described so a client picker can render input fields.
    og = next(p for p in prompts if p["name"] == "orchestrate_goal")
    arg_names = {a["name"] for a in og["arguments"]}
    assert "goal" in arg_names


def test_http_transport_gets_prompt_with_arguments(_clean_env: None) -> None:
    from bernstein.mcp.remote_transport import (
        RemoteMCPConfig,
        StreamableHTTPTransport,
    )

    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg)
    resp = _post_jsonrpc(
        transport,
        "prompts/get",
        {"name": "orchestrate_goal", "arguments": {"goal": "ship X", "role": "qa"}},
    )
    result = resp["result"]
    assert result["messages"], "expected at least one message"
    msg = result["messages"][0]
    assert msg["role"] == "user"
    assert msg["content"]["type"] == "text"
    text = msg["content"]["text"]
    assert "ship X" in text
    assert "qa" in text


def test_http_transport_prompts_get_unknown_name_errors(_clean_env: None) -> None:
    from bernstein.mcp.remote_transport import (
        RemoteMCPConfig,
        StreamableHTTPTransport,
    )

    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "prompts/get",
            "id": 7,
            "params": {"name": "nope"},
        }
    ).encode()
    status, _, resp_body = asyncio.run(transport.handle_request("POST", "/mcp", {}, body))
    assert status == 200
    payload = json.loads(resp_body)
    assert "error" in payload
    assert "nope" in payload["error"]["message"]


def test_http_initialize_advertises_prompts_capability(_clean_env: None) -> None:
    from bernstein.mcp.remote_transport import (
        RemoteMCPConfig,
        StreamableHTTPTransport,
    )

    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg)
    resp = _post_jsonrpc(
        transport,
        "initialize",
        {"clientInfo": {"name": "test"}},
    )
    caps = resp["result"]["capabilities"]
    assert "prompts" in caps
