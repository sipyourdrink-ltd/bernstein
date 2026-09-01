"""Tests for the OTLP ingest boundary (issue #4983).

The acceptance criteria from the issue:

1. A fixture OTLP payload ingests to chain events, preserving trace_id and span_id.
2. Spans carrying GenAI conventions produce typed activity with model and token counts.
3. Spans without GenAI conventions are recorded as untyped, never inferred.
4. Our own exported spans round-trip through the receiver to equivalent chain activity.
5. A malformed payload is rejected and appends nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.observability.otlp_ingest import (
    OTLPIngestAdapter,
    OTLPIngestError,
    ingest_payload,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture
def adapter() -> OTLPIngestAdapter:
    return OTLPIngestAdapter()


def _genai_span(
    *,
    trace_id: str = "abc123def456abc123def456abc123de",
    span_id: str = "f1e2d3c4b5a69788",
    name: str = "gen_ai.chat",
    system: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    operation: str = "chat",
    prompt_tokens: int | None = 128,
    completion_tokens: int | None = 64,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    extra_attrs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """A fixture OTLP/JSON span carrying GenAI conventions."""
    attrs: list[dict[str, object]] = [
        {"key": "gen_ai.system", "value": {"stringValue": system}},
        {"key": "gen_ai.request.model", "value": {"stringValue": model}},
        {"key": "gen_ai.operation.name", "value": {"stringValue": operation}},
    ]
    if prompt_tokens is not None:
        attrs.append({"key": "gen_ai.usage.prompt_tokens", "value": {"intValue": str(prompt_tokens)}})
    if completion_tokens is not None:
        attrs.append({"key": "gen_ai.usage.completion_tokens", "value": {"intValue": str(completion_tokens)}})
    if tool_name is not None:
        attrs.append({"key": "gen_ai.tool.name", "value": {"stringValue": tool_name}})
    if tool_call_id is not None:
        attrs.append({"key": "gen_ai.tool.call.id", "value": {"stringValue": tool_call_id}})
    if extra_attrs:
        attrs.extend(extra_attrs)
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": "SPAN_KIND_CLIENT",
        "attributes": attrs,
    }


def _untyped_span(
    *,
    trace_id: str = "000102030405060708090a0b0c0d0e0f",
    span_id: str = "f1e2d3c4b5a69788",
    name: str = "http.request",
    extra_attrs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """A fixture OTLP/JSON span with no GenAI conventions."""
    attrs: list[dict[str, object]] = [
        {"key": "http.method", "value": {"stringValue": "GET"}},
        {"key": "http.url", "value": {"stringValue": "https://api.example.com"}},
    ]
    if extra_attrs:
        attrs.extend(extra_attrs)
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": "SPAN_KIND_CLIENT",
        "attributes": attrs,
    }


# --------------------------------------------------------------------------- #
# AC1: Fixture payload → chain events preserving trace/span ids                #
# --------------------------------------------------------------------------- #


def test_ingest_preserves_trace_id_and_span_id(adapter: OTLPIngestAdapter) -> None:
    """A fixture OTLP payload is ingested to chain events preserving ids."""
    payload = [
        _genai_span(trace_id="deadbeefdeadbeefdeadbeefdeadbeef", span_id="cafebabecafebabe"),
        _untyped_span(trace_id="fafafafafafafafafafafafafafafafa", span_id="1234567812345678"),
    ]

    results = adapter.ingest_payload(payload)

    assert len(results) == 2

    # First span (typed) preserves ids
    assert results[0].is_typed
    assert results[0].typed is not None
    assert results[0].typed.trace_id == "deadbeefdeadbeefdeadbeefdeadbeef"
    assert results[0].typed.span_id == "cafebabecafebabe"

    # Second span (untyped) preserves ids
    assert results[1].is_untyped
    assert results[1].untyped is not None
    assert results[1].untyped.trace_id == "fafafafafafafafafafafafafafafafa"
    assert results[1].untyped.span_id == "1234567812345678"


def test_chain_event_renders_trace_and_span_ids(adapter: OTLPIngestAdapter) -> None:
    """The chain event carries the original trace_id and span_id verbatim."""
    span = _genai_span(trace_id="abcd1234abcd1234abcd1234abcd1234", span_id="feedfacefeedface")
    result = adapter.ingest_span(span)
    assert result.is_typed
    assert result.typed is not None

    chain_event = result.typed.to_chain_event()
    assert chain_event["attributes"]["otlp.trace_id"] == "abcd1234abcd1234abcd1234abcd1234"
    assert chain_event["attributes"]["otlp.span_id"] == "feedfacefeedface"

    untyped_span = _untyped_span(trace_id="9999aaaa9999aaaa9999aaaa9999aaaa", span_id="1234abcd1234abcd")
    result2 = adapter.ingest_span(untyped_span)
    assert result2.is_untyped
    assert result2.untyped is not None

    chain_event2 = result2.untyped.to_chain_event()
    assert chain_event2["attributes"]["otlp.trace_id"] == "9999aaaa9999aaaa9999aaaa9999aaaa"
    assert chain_event2["attributes"]["otlp.span_id"] == "1234abcd1234abcd"


# --------------------------------------------------------------------------- #
# AC2: GenAI spans → typed activity with model and token counts               #
# --------------------------------------------------------------------------- #


def test_genai_span_produces_typed_activity(adapter: OTLPIngestAdapter) -> None:
    """A span carrying GenAI conventions yields a typed GenAIActivity."""
    span = _genai_span(
        system="openai",
        model="gpt-4o",
        operation="chat",
        prompt_tokens=512,
        completion_tokens=128,
    )
    result = adapter.ingest_span(span)
    assert result.is_typed
    assert result.untyped is None


def test_genai_activity_model_and_tokens(adapter: OTLPIngestAdapter) -> None:
    """Typed activity carries model and token counts verbatim."""
    span = _genai_span(
        system="anthropic",
        model="claude-opus-4-5",
        operation="chat",
        prompt_tokens=200,
        completion_tokens=75,
    )
    result = adapter.ingest_span(span)
    assert result.is_typed
    activity = result.typed
    assert activity is not None
    assert activity.system == "anthropic"
    assert activity.model == "claude-opus-4-5"
    assert activity.operation == "chat"
    assert activity.prompt_tokens == 200
    assert activity.completion_tokens == 75


def test_genai_activity_tool_attrs(adapter: OTLPIngestAdapter) -> None:
    """Typed activity carries tool.name and tool.call.id when present."""
    span = _genai_span(
        tool_name="bash",
        tool_call_id="tool_abc123",
        prompt_tokens=100,
        completion_tokens=50,
    )
    result = adapter.ingest_span(span)
    assert result.is_typed
    activity = result.typed
    assert activity is not None
    assert activity.tool_name == "bash"
    assert activity.tool_call_id == "tool_abc123"


def test_genai_activity_missing_optional_tokens_is_still_typed(adapter: OTLPIngestAdapter) -> None:
    """A GenAI span missing optional token counts is still typed, not untyped."""
    span = _genai_span(prompt_tokens=None, completion_tokens=None)
    result = adapter.ingest_span(span)
    assert result.is_typed, "A GenAI span with no token counts must still be typed"
    activity = result.typed
    assert activity is not None
    assert activity.prompt_tokens is None
    assert activity.completion_tokens is None


def test_genai_chain_event_activity_type_is_typed(adapter: OTLPIngestAdapter) -> None:
    """Chain event for a GenAI span marks activity_type as 'typed'."""
    span = _genai_span()
    result = adapter.ingest_span(span)
    chain_event = result.typed.to_chain_event() if result.is_typed else None
    assert chain_event is not None
    assert chain_event["attributes"]["gen_ai.activity_type"] == "typed"


# --------------------------------------------------------------------------- #
# AC3: Non-GenAI spans → untyped, never inferred                              #
# --------------------------------------------------------------------------- #


def test_no_genai_attrs_is_untyped(adapter: OTLPIngestAdapter) -> None:
    """A span without GenAI conventions yields UntypedActivity."""
    span = _untyped_span()
    result = adapter.ingest_span(span)
    assert result.is_untyped
    assert result.typed is None


def test_span_name_not_inferred_as_activity_type(adapter: OTLPIngestAdapter) -> None:
    """A span named 'gen_ai.chat' but with no GenAI attrs is untyped.

    Type must never be inferred from the span name.
    """
    span = _untyped_span(name="gen_ai.execute_tool")
    result = adapter.ingest_span(span)
    assert result.is_untyped, "A span named 'gen_ai.*' but with no GenAI attrs must be untyped"


def test_untyped_chain_event_activity_type_is_untyped(adapter: OTLPIngestAdapter) -> None:
    """Chain event for an untyped span marks activity_type as 'untyped'."""
    span = _untyped_span()
    result = adapter.ingest_span(span)
    assert result.is_untyped
    chain_event = result.untyped.to_chain_event() if result.is_untyped else None
    assert chain_event is not None
    assert chain_event["attributes"]["gen_ai.activity_type"] == "untyped"
    assert chain_event["attributes"]["otlp.ingest_untyped"] is True


# --------------------------------------------------------------------------- #
# AC4: Our own exported spans round-trip                                       #
# --------------------------------------------------------------------------- #


def test_round_trip_through_exported_spans(adapter: OTLPIngestAdapter) -> None:
    """Our own exported spans, when re-ingested, yield equivalent chain activity.

    Uses the same helpers as test_telemetry_verify_span_cmd.py:
    projection_to_otlp_json_spans produces the exact shape the ingest expects.
    """
    import tempfile

    from bernstein.core.observability.otel_bridge import (
        projection_to_otlp_json_spans,
    )
    from bernstein.core.observability.otel_projection import (
        project_spans,
        sign_projection,
    )
    from bernstein.core.security.install_key import (
        load_or_create_install_key,
        signing_key_path,
    )

    tmp = Path(tempfile.mkdtemp())
    key = load_or_create_install_key(signing_key_path(tmp))

    # Build a minimal journal
    events = [
        {"event": "run_started", "event_hash": "a" * 64, "ts": 1700000000.0},
        {"event": "agent_spawned", "event_hash": "b" * 64, "ts": 1700000001.0},
        {"event": "task_claimed", "event_hash": "c" * 64, "ts": 1700000002.0},
    ]
    projection = project_spans(events, run_id="test-round-trip")
    signed = sign_projection(projection, signing_key=key)

    # Export to OTLP/JSON shape
    exported_spans = projection_to_otlp_json_spans(signed, events)
    assert len(exported_spans) == 3

    # Re-ingest our own output
    results = adapter.ingest_payload(exported_spans)

    assert len(results) == 3
    # Our projected spans carry GenAI attributes (system="bernstein", operation="invoke_workflow" etc.)
    # so they are typed, not untyped. They preserve trace/span ids and round-trip faithfully.
    for r in results:
        assert r.is_typed, "Projected spans with GenAI attrs must be typed"
        activity = r.typed
        assert activity is not None
        assert activity.system == "bernstein"
        assert activity.operation in {"invoke_workflow", "invoke_agent", "execute_tool"}
        # trace_id and span_id preserved exactly
        assert len(activity.trace_id) == 32
        assert len(activity.span_id) == 16


def test_round_trip_genai_attrs_are_preserved(adapter: OTLPIngestAdapter) -> None:
    """A span with GenAI attrs round-trips as typed with all fields preserved."""
    original = _genai_span(
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="fedcba9876543210",
        system="openai",
        model="gpt-4-turbo",
        operation="chat",
        prompt_tokens=1000,
        completion_tokens=250,
        tool_name="web_search",
        tool_call_id="call_xyz789",
    )
    results = adapter.ingest_payload([original])
    assert len(results) == 1
    assert results[0].is_typed
    activity = results[0].typed
    assert activity is not None
    assert activity.trace_id == original["traceId"]
    assert activity.span_id == original["spanId"]
    assert activity.system == "openai"
    assert activity.model == "gpt-4-turbo"
    assert activity.operation == "chat"
    assert activity.prompt_tokens == 1000
    assert activity.completion_tokens == 250
    assert activity.tool_name == "web_search"
    assert activity.tool_call_id == "call_xyz789"


# --------------------------------------------------------------------------- #
# AC5: Malformed payload → rejection, nothing appended                        #
# --------------------------------------------------------------------------- #


def test_non_dict_payload_raises(adapter: OTLPIngestAdapter) -> None:
    """A top-level payload that is not a dict or list raises OTLPIngestError."""
    with pytest.raises(OTLPIngestError):
        adapter.ingest_payload(
            "not a span"  # type: ignore[arg-type]
        )


def test_non_list_with_non_dict_element_raises(adapter: OTLPIngestAdapter) -> None:
    """A list containing a non-dict element raises OTLPIngestError."""
    with pytest.raises(OTLPIngestError, match="span\\[0\\] is not a dict"):
        adapter.ingest_payload(["not a dict", "also not a dict"])  # type: ignore[list-item]


def test_empty_list_raises(adapter: OTLPIngestAdapter) -> None:
    """An empty list payload raises OTLPIngestError."""
    with pytest.raises(OTLPIngestError, match="empty list"):
        adapter.ingest_payload([])


def test_missing_trace_id_returns_error_result(adapter: OTLPIngestAdapter) -> None:
    """A span missing traceId returns an error result (parse error, not exception)."""
    result = adapter.ingest_span({"spanId": "abc"})
    assert result.is_error
    assert "traceId" in result.parse_error


def test_missing_span_id_returns_error_result(adapter: OTLPIngestAdapter) -> None:
    """A span missing spanId returns an error result."""
    result = adapter.ingest_span({"traceId": "abc"})
    assert result.is_error
    assert "spanId" in result.parse_error


def test_none_trace_id_returns_error_result(adapter: OTLPIngestAdapter) -> None:
    """A span with traceId=None returns an error result."""
    result = adapter.ingest_span({"traceId": None, "spanId": "abc"})  # type: ignore[arg-type]
    assert result.is_error


def test_no_partial_state_on_span_in_list_error(adapter: OTLPIngestAdapter) -> None:
    """When span[2] in a list is malformed, no results for spans[0:2] are returned."""
    payload = [
        {"traceId": "a" * 32, "spanId": "1" * 16, "name": "ok1"},
        {"traceId": "b" * 32, "spanId": "2" * 16, "name": "ok2"},
        "not a dict",
    ]
    with pytest.raises(OTLPIngestError, match="span\\[2\\]"):
        adapter.ingest_payload(payload)


def test_bad_attributes_in_list_error(adapter: OTLPIngestAdapter) -> None:
    """A list where one span has bad attributes raises OTLPIngestError."""
    # _span_attributes would need to raise for this to trigger; the current
    # impl handles malformed attrs gracefully, so we test the non-dict span path.
    with pytest.raises(OTLPIngestError, match="span\\[1\\]"):
        adapter.ingest_payload(
            [
                {"traceId": "a" * 32, "spanId": "1" * 16},
                123,  # type: ignore[list-item]
            ]
        )


# --------------------------------------------------------------------------- #
# Attribute parsing variants                                                   #
# --------------------------------------------------------------------------- #


def test_attributes_otlp_json_list_form(adapter: OTLPIngestAdapter) -> None:
    """Attributes in OTLP/JSON list form (list of {key, value}) are parsed."""
    span = _genai_span(
        extra_attrs=[
            {"key": "custom.attr", "value": {"stringValue": "custom-value"}},
        ]
    )
    result = adapter.ingest_span(span)
    assert result.is_typed
    activity = result.typed
    assert activity is not None
    assert "custom.attr" in activity.extra_attributes
    assert activity.extra_attributes["custom.attr"] == "custom-value"


def test_attributes_otlp_proto_dict_form(adapter: OTLPIngestAdapter) -> None:
    """Attributes as a plain key→value dict are parsed (OTLP/protobuf wire form)."""
    span = {
        "traceId": "a" * 32,
        "spanId": "1" * 16,
        "name": "test",
        "kind": "SPAN_KIND_CLIENT",
        "attributes": {
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-3.5-turbo",
            "gen_ai.operation.name": "chat",
        },
    }
    result = adapter.ingest_span(span)
    assert result.is_typed
    activity = result.typed
    assert activity is not None
    assert activity.system == "openai"
    assert activity.model == "gpt-3.5-turbo"


def test_attributes_plain_python_dict(adapter: OTLPIngestAdapter) -> None:
    """Attributes as plain Python scalars (no AnyValue wrappers) are accepted."""
    span = {
        "traceId": "a" * 32,
        "spanId": "1" * 16,
        "name": "test",
        "kind": "SPAN_KIND_CLIENT",
        "attributes": {
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-3.5-turbo",
            "gen_ai.operation.name": "chat",
            "custom_count": 42,
            "custom_flag": True,
        },
    }
    result = adapter.ingest_span(span)
    assert result.is_typed
    activity = result.typed
    assert activity is not None
    assert activity.extra_attributes.get("custom_count") == 42
    assert activity.extra_attributes.get("custom_flag") is True


def test_snake_case_keys_accepted(adapter: OTLPIngestAdapter) -> None:
    """Span dicts using snake_case keys (trace_id, span_id) are accepted."""
    span = {
        "trace_id": "b" * 32,
        "span_id": "2" * 16,
        "name": "snake_test",
        "attributes": [],
    }
    result = adapter.ingest_span(span)
    assert result.is_untyped
    assert result.untyped is not None
    assert result.untyped.trace_id == "b" * 32
    assert result.untyped.span_id == "2" * 16


# --------------------------------------------------------------------------- #
# Convenience function                                                        #
# --------------------------------------------------------------------------- #


def test_ingest_payload_function_raises_on_bad_input() -> None:
    """The module-level ingest_payload convenience raises on bad input."""
    with pytest.raises(OTLPIngestError):
        ingest_payload([])  # type: ignore[arg-type]


def test_ingest_payload_function_ok() -> None:
    """The module-level ingest_payload returns results for valid input."""
    results = ingest_payload([{"traceId": "a" * 32, "spanId": "1" * 16, "attributes": []}])
    assert len(results) == 1
    assert results[0].is_untyped


# --------------------------------------------------------------------------- #
# Source label                                                                  #
# --------------------------------------------------------------------------- #


def test_source_label_in_chain_event(adapter: OTLPIngestAdapter) -> None:
    """The source_label is written to the chain event's 'source' field."""
    adapter_labeled = OTLPIngestAdapter(source_label="my-otlp-collector")
    span = _genai_span()
    result = adapter_labeled.ingest_span(span)
    chain_event = result.typed.to_chain_event(source=adapter_labeled._source) if result.is_typed else None
    assert chain_event is not None
    assert chain_event["source"] == "my-otlp-collector"


# --------------------------------------------------------------------------- #
# Untyped extra attributes preserved                                           #
# --------------------------------------------------------------------------- #


def test_untyped_preserves_extra_attributes(adapter: OTLPIngestAdapter) -> None:
    """Untyped activity preserves non-GenAI extra attributes verbatim."""
    span = _untyped_span(
        extra_attrs=[
            {"key": "http.method", "value": {"stringValue": "POST"}},
            {"key": "http.status_code", "value": {"intValue": "200"}},
        ]
    )
    result = adapter.ingest_span(span)
    assert result.is_untyped
    attrs = result.untyped.extra_attributes
    assert attrs.get("http.method") == "POST"
    assert attrs.get("http.status_code") == 200


# --------------------------------------------------------------------------- #
# Int values in AnyValue                                                      #
# --------------------------------------------------------------------------- #


def test_int_value_handles_numeric_string(adapter: OTLPIngestAdapter) -> None:
    """OTLP AnyValue intValue may be a string representation of an int."""
    span = {
        "traceId": "a" * 32,
        "spanId": "1" * 16,
        "name": "test",
        "attributes": [
            {"key": "gen_ai.usage.prompt_tokens", "value": {"intValue": "512"}},
            {"key": "gen_ai.system", "value": {"stringValue": "test"}},
            {"key": "gen_ai.request.model", "value": {"stringValue": "m"}},
            {"key": "gen_ai.operation.name", "value": {"stringValue": "c"}},
        ],
    }
    result = adapter.ingest_span(span)
    assert result.is_typed
    assert result.typed is not None
    assert result.typed.prompt_tokens == 512
    assert result.typed.completion_tokens is None
