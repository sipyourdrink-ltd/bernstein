"""Structured tool results and declared output schemas (#3086).

The three most-polled tools declare an ``outputSchema`` and return
``structuredContent``: a client renders run state natively and validates a
result programmatically, instead of re-parsing a JSON string out of a text
blob on every poll. The text content block is unchanged, including the
``_meter`` envelope, so no current consumer breaks; the declared schema
describes the envelope exactly as it is emitted for the live meter state.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import jsonschema
import pytest

from bernstein.mcp.server import create_mcp_server

_STRUCTURED = ("bernstein_run", "bernstein_status", "bernstein_run_status")


@pytest.fixture
def _meter_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_MCP_COST_METER", raising=False)


@pytest.fixture
def _meter_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "0")


async def _wire_tools_async(mcp: Any) -> dict[str, Any]:
    from mcp.types import ListToolsRequest

    handler = mcp._mcp_server.request_handlers[ListToolsRequest]
    response = await handler(ListToolsRequest(method="tools/list"))
    return {tool.name: tool for tool in response.root.tools}


def _wire_tools(mcp: Any) -> dict[str, Any]:
    return asyncio.run(_wire_tools_async(mcp))


def _seed_journal(tmp_path: Any) -> None:
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.tasks.checkpoint_retry import task_run_id

    journal = EventJournal(task_run_id("abc123"), tmp_path / ".sdd")
    journal.record("run_started", goal="g")
    journal.record("run_completed", result="done")


async def _poll(mcp: Any, run_id: str = "abc123") -> Any:
    return await mcp.call_tool("bernstein_run_status", {"run_id": run_id})


# ---------------------------------------------------------------------------
# Advertised output schemas
# ---------------------------------------------------------------------------


def test_structured_tools_declare_an_output_schema(_meter_on: None) -> None:
    mcp = create_mcp_server(server_url="http://localhost:8052")
    tools = _wire_tools(mcp)
    for name in _STRUCTURED:
        assert tools[name].outputSchema is not None, name
        # With the meter on, the declared schema is the envelope as emitted.
        assert tools[name].outputSchema["required"] == ["result", "_meter"], name


def test_output_schema_describes_the_bare_payload_when_meter_off(_meter_off: None) -> None:
    mcp = create_mcp_server(server_url="http://localhost:8052")
    tools = _wire_tools(mcp)
    for name in _STRUCTURED:
        schema = tools[name].outputSchema
        assert schema is not None, name
        assert "_meter" not in json.dumps(schema.get("required", [])), name


def test_unstructured_tools_keep_the_sdk_derived_schema(_meter_on: None) -> None:
    """Only the structured tools advertise the envelope schema.

    The pinned SDK derives a ``{"result": string}`` wrapper for every
    ``-> str`` tool; that derived shape must not be replaced by the meter
    envelope on tools that do not emit it.
    """
    mcp = create_mcp_server(server_url="http://localhost:8052")
    tools = _wire_tools(mcp)
    for name in ("bernstein_approve", "load_skill"):
        schema = tools[name].outputSchema
        assert schema is None or schema.get("required") != ["result", "_meter"], name


# ---------------------------------------------------------------------------
# Live results validate against their own declared schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_poll_result_validates_against_its_declared_schema(
    _meter_on: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_journal(tmp_path)
    mcp = create_mcp_server(server_url="http://localhost:8052")
    declared = (await _wire_tools_async(mcp))["bernstein_run_status"].outputSchema
    result = await _poll(mcp)
    assert result.structuredContent is not None
    jsonschema.Draft7Validator(declared).validate(result.structuredContent)
    # The envelope is emitted because the meter is on, and the schema said so.
    assert "_meter" in result.structuredContent


@pytest.mark.asyncio
async def test_live_poll_result_validates_with_the_meter_off(
    _meter_off: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_journal(tmp_path)
    mcp = create_mcp_server(server_url="http://localhost:8052")
    declared = (await _wire_tools_async(mcp))["bernstein_run_status"].outputSchema
    result = await _poll(mcp)
    assert result.structuredContent is not None
    jsonschema.Draft7Validator(declared).validate(result.structuredContent)
    assert "_meter" not in result.structuredContent


# ---------------------------------------------------------------------------
# The text block is unchanged; structuredContent is its parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_block_is_the_envelope_and_structured_is_its_parse(
    _meter_on: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_journal(tmp_path)
    mcp = create_mcp_server(server_url="http://localhost:8052")
    result = await _poll(mcp)
    text = result.content[0].text
    parsed = json.loads(text)
    assert parsed == result.structuredContent
    assert set(parsed) == {"result", "_meter"}


# ---------------------------------------------------------------------------
# Run-handle fields are first-class typed fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_handle_fields_are_first_class(
    _meter_on: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_journal(tmp_path)
    mcp = create_mcp_server(server_url="http://localhost:8052")
    result = await _poll(mcp)
    handle = result.structuredContent["result"]
    for field in ("taskId", "runId", "status", "journalHead", "chainHead", "receiptHash", "pollToken"):
        assert isinstance(handle[field], str), field
    assert handle["status"] == "completed"


def test_wire_schema_is_generated_from_the_wire_body() -> None:
    """The advertised handle schema cannot drift from what to_wire emits."""
    from bernstein.core.protocols.mcp.tasks_extension import RunHandle

    handle = RunHandle.from_journal(task_id="t", run_id="t", events=[], chain_head="")
    body = handle.to_wire()
    schema = RunHandle.wire_schema()
    assert set(schema["properties"]) == set(body)
    jsonschema.Draft7Validator(schema).validate(body)


# ---------------------------------------------------------------------------
# Determinism: no wall clock, no model field, byte-identical projections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_projections_of_one_journal_are_byte_identical(
    _meter_on: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hashed region of the structured output carries no wall clock.

    The ``_meter`` record does carry a timestamp, but it sits outside
    ``result`` and outside every hashed region; the handle body itself is a
    pure projection, so two polls of an unchanged journal serialize to the
    same bytes.
    """
    monkeypatch.chdir(tmp_path)
    _seed_journal(tmp_path)
    mcp = create_mcp_server(server_url="http://localhost:8052")
    first = await _poll(mcp)
    second = await _poll(mcp)
    first_bytes = json.dumps(first.structuredContent["result"], sort_keys=True).encode()
    second_bytes = json.dumps(second.structuredContent["result"], sort_keys=True).encode()
    assert first_bytes == second_bytes
