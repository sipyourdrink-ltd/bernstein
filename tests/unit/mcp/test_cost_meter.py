"""Tests for the per-call cost-meter envelope (issue #1674).

Covers:

  * the meter toggle (``BERNSTEIN_MCP_COST_METER``);
  * the :func:`measure_call` context manager (latency, cost, status);
  * :func:`wrap_envelope` shape on success, failure, and non-JSON payloads;
  * the FastMCP server wrapping every tool response in the envelope;
  * the streamable HTTP transport emitting the same envelope shape.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bernstein.mcp.cost_meter import (
    CallMeter,
    cost_meter_enabled,
    measure_call,
    wrap_envelope,
)


@pytest.fixture
def _meter_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_MCP_COST_METER", raising=False)


@pytest.fixture
def _meter_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "0")


# ---------------------------------------------------------------------------
# Toggle
# ---------------------------------------------------------------------------


def test_meter_enabled_by_default(_meter_on: None) -> None:
    assert cost_meter_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", "False"])
def test_meter_disabled_by_falsey_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", value)
    assert cost_meter_enabled() is False


def test_meter_enabled_by_truthy_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "1")
    assert cost_meter_enabled() is True


# ---------------------------------------------------------------------------
# CallMeter / measure_call
# ---------------------------------------------------------------------------


def test_measure_call_records_latency_and_ok() -> None:
    with measure_call("bernstein_status") as meter:
        pass
    record = meter.to_dict()
    assert record["tool"] == "bernstein_status"
    assert record["ok"] is True
    assert record["latency_ms"] >= 0.0
    assert record["cost_usd"] == 0.0
    assert "call_id" in record
    assert record["ts"].endswith("Z")


def test_measure_call_records_failure_and_reraises() -> None:
    meter_seen: CallMeter | None = None
    with pytest.raises(ValueError, match="boom"):
        with measure_call("bernstein_run") as meter:
            meter_seen = meter
            raise ValueError("boom")
    assert meter_seen is not None
    record = meter_seen.to_dict()
    assert record["ok"] is False
    assert record["error"] == "boom"
    assert record["latency_ms"] >= 0.0


def test_add_cost_accumulates() -> None:
    with measure_call("x") as meter:
        meter.add_cost(0.01)
        meter.add_cost(0.02)
    assert meter.to_dict()["cost_usd"] == pytest.approx(0.03)


def test_call_ids_are_unique() -> None:
    with measure_call("a") as m1:
        pass
    with measure_call("a") as m2:
        pass
    assert m1.call_id != m2.call_id


# ---------------------------------------------------------------------------
# wrap_envelope
# ---------------------------------------------------------------------------


def test_wrap_envelope_nests_json_result(_meter_on: None) -> None:
    with measure_call("bernstein_status") as meter:
        pass
    out = wrap_envelope('{"total": 5}', meter)
    parsed = json.loads(out)
    assert parsed["result"] == {"total": 5}
    assert parsed["_meter"]["tool"] == "bernstein_status"


def test_wrap_envelope_carries_non_json_verbatim(_meter_on: None) -> None:
    with measure_call("x") as meter:
        pass
    out = wrap_envelope("not json", meter)
    assert json.loads(out)["result"] == "not json"


def test_wrap_envelope_passthrough_when_disabled(_meter_off: None) -> None:
    with measure_call("x") as meter:
        pass
    payload = '{"total": 5}'
    assert wrap_envelope(payload, meter) == payload


# ---------------------------------------------------------------------------
# FastMCP server integration
# ---------------------------------------------------------------------------


def _call(tool: str, args: dict | None = None) -> str:
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(tier="standard")
    result = asyncio.run(mcp.call_tool(tool, args or {}))
    if hasattr(result, "content"):
        # Structured tools (#3086) answer with a CallToolResult.
        return result.content[0].text
    # FastMCP returns a (content, structured) tuple in recent versions, or a
    # content list in older ones; normalise to the text payload.
    content = result[0] if isinstance(result, tuple) else result
    return content[0].text


def test_server_health_alias_response_is_enveloped(_meter_on: None) -> None:
    # The health alias (#3087) is metered like any tool: the deprecation
    # wrapper sits inside the meter envelope, and the liveness body inside it.
    parsed = json.loads(_call("bernstein_health"))
    assert parsed["result"]["result"] == {"status": "ok"}
    assert parsed["result"]["replacement"] == "bernstein_status"
    assert parsed["_meter"]["tool"] == "bernstein_health"
    assert parsed["_meter"]["ok"] is True


def test_server_response_bare_when_disabled(_meter_off: None) -> None:
    parsed = json.loads(_call("bernstein_health"))
    assert parsed["result"] == {"status": "ok"}
    assert parsed["deprecated"] is True


# ---------------------------------------------------------------------------
# Streamable HTTP transport integration
# ---------------------------------------------------------------------------


def test_http_transport_health_enveloped(_meter_on: None) -> None:
    from bernstein.mcp.remote_transport import (
        RemoteMCPConfig,
        StreamableHTTPTransport,
    )

    cfg = RemoteMCPConfig(host="127.0.0.1", auth_type="none")
    transport = StreamableHTTPTransport(config=cfg)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {"name": "bernstein_health", "arguments": {}},
        }
    ).encode()
    status, _, resp_body = asyncio.run(transport.handle_request("POST", "/mcp", {}, body))
    assert status == 200
    text = json.loads(resp_body)["result"]["content"][0]["text"]
    parsed = json.loads(text)
    # bernstein_health is a deprecated alias (#3087): the liveness body sits
    # under the deprecation wrapper, which sits under the meter envelope.
    assert parsed["result"]["result"]["status"] == "ok"
    assert parsed["result"]["replacement"] == "bernstein_status"
    assert parsed["_meter"]["tool"] == "bernstein_health"
