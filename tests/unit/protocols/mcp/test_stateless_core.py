"""Tests for the stateless MCP protocol core (issue #2307).

The stateless core drops the ``initialize`` handshake and ``Mcp-Session-Id``:
client capabilities ride in a per-request ``_meta`` field, cross-call ordering
lives in the event journal rather than a session store, and an
``InputRequiredResult`` retry carries a base64 ``requestState`` so any server
instance can process the retry. W3C Trace Context values are derived from
content hashes so two replays emit byte-identical ``_meta``.

These are pure/deterministic tests: no clock, socket, or session store.
"""

from __future__ import annotations

import base64
import json

import pytest

from bernstein.core.protocols.mcp.stateless_core import (
    DEPRECATED_CAPABILITIES,
    CacheDirective,
    CacheReference,
    InputRequiredResult,
    StatelessCallRecord,
    build_request_meta,
    compat_shim_active,
    decode_request_state,
    derive_span_id,
    derive_trace_id,
    encode_request_state,
    format_traceparent,
    resolve_sampling_in_orchestrator,
)


def _client_caps() -> dict[str, object]:
    return {"tools": {"listChanged": True}, "roots": {"listChanged": False}}


class TestRequestMeta:
    def test_meta_carries_client_caps_without_session_id(self) -> None:
        # AC1: caps ride in _meta and no session id is present.
        meta = build_request_meta(
            method="tools/call",
            params_content_hash="a" * 64,
            run_root_hash="b" * 64,
            call_index=0,
            client_capabilities=_client_caps(),
        )
        assert meta["client"]["capabilities"] == _client_caps()
        assert "Mcp-Session-Id" not in meta
        assert "sessionId" not in meta
        assert "session_id" not in meta

    def test_meta_carries_w3c_trace_context(self) -> None:
        meta = build_request_meta(
            method="tools/call",
            params_content_hash="a" * 64,
            run_root_hash="b" * 64,
            call_index=3,
            client_capabilities=_client_caps(),
        )
        assert "traceparent" in meta
        assert "tracestate" in meta
        assert "baggage" in meta
        # traceparent = version-traceid-spanid-flags
        parts = meta["traceparent"].split("-")
        assert len(parts) == 4
        assert parts[0] == "00"
        assert len(parts[1]) == 32
        assert len(parts[2]) == 16
        assert parts[3] == "01"

    def test_two_replays_emit_byte_identical_trace_context(self) -> None:
        # AC2: same inputs -> byte-identical traceparent/tracestate.
        kwargs = {
            "method": "tools/call",
            "params_content_hash": "c" * 64,
            "run_root_hash": "d" * 64,
            "call_index": 7,
            "client_capabilities": _client_caps(),
        }
        a = build_request_meta(**kwargs)
        b = build_request_meta(**kwargs)
        assert a["traceparent"] == b["traceparent"]
        assert a["tracestate"] == b["tracestate"]
        assert a["baggage"] == b["baggage"]
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_span_id_is_content_derived_not_random(self) -> None:
        first = build_request_meta(
            method="tools/call",
            params_content_hash="e" * 64,
            run_root_hash="f" * 64,
            call_index=1,
            client_capabilities=_client_caps(),
        )["traceparent"].split("-")[2]
        # Different call index -> different span id (ordered), same inputs same id.
        second = build_request_meta(
            method="tools/call",
            params_content_hash="e" * 64,
            run_root_hash="f" * 64,
            call_index=2,
            client_capabilities=_client_caps(),
        )["traceparent"].split("-")[2]
        assert first != second
        assert first == derive_span_id(params_content_hash="e" * 64, call_index=1)

    def test_trace_id_shared_across_calls_of_one_run(self) -> None:
        a = build_request_meta(
            method="tools/call",
            params_content_hash="1" * 64,
            run_root_hash="9" * 64,
            call_index=0,
            client_capabilities=_client_caps(),
        )["traceparent"].split("-")[1]
        b = build_request_meta(
            method="resources/read",
            params_content_hash="2" * 64,
            run_root_hash="9" * 64,
            call_index=5,
            client_capabilities=_client_caps(),
        )["traceparent"].split("-")[1]
        assert a == b == derive_trace_id(run_root_hash="9" * 64)

    def test_format_traceparent_shape(self) -> None:
        tp = format_traceparent(trace_id="a" * 32, span_id="b" * 16)
        assert tp == f"00-{'a' * 32}-{'b' * 16}-01"


class TestRequestState:
    def test_round_trip_base64(self) -> None:
        state = {"tool": "search", "cursor": 42, "partial": ["x", "y"]}
        encoded = encode_request_state(state)
        # base64 of canonical JSON
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        assert json.loads(raw) == state
        assert decode_request_state(encoded) == state

    def test_encoding_is_deterministic(self) -> None:
        state = {"b": 2, "a": 1}
        assert encode_request_state(state) == encode_request_state({"a": 1, "b": 2})

    def test_decode_rejects_malformed(self) -> None:
        with pytest.raises(ValueError, match="requestState"):
            decode_request_state("not base64 !!!")


class TestInputRequiredRetry:
    def test_retry_completes_on_a_different_instance(self) -> None:
        # AC3: a retry produced by instance A is processed by instance B with
        # no shared memory - only the base64 requestState echoed back.
        result = InputRequiredResult(
            prompt="need an API host",
            request_state={"tool": "fetch", "await_field": "host"},
        )
        wire = result.to_wire()
        assert wire["type"] == "input_required"
        assert "requestState" in wire

        # Instance B has never seen instance A's memory. It rebuilds solely
        # from the echoed requestState.
        supplied = {"host": "example.test"}
        rebuilt = InputRequiredResult.resume_from_wire(wire, supplied_input=supplied)
        assert rebuilt["tool"] == "fetch"
        assert rebuilt["await_field"] == "host"
        assert rebuilt["input"] == supplied

    def test_resume_rejects_tampered_state(self) -> None:
        with pytest.raises(ValueError, match="requestState"):
            InputRequiredResult.resume_from_wire(
                {"type": "input_required", "requestState": "@@@"},
                supplied_input={},
            )


class TestSamplingInOrchestrator:
    def test_sampling_resolved_locally_no_client_callback(self) -> None:
        # Migrate off Sampling: the decision is resolved in-orchestrator with a
        # deterministic resolver, never an LLM callback into the client.
        calls: list[dict[str, object]] = []

        def resolver(request: dict[str, object]) -> str:
            calls.append(request)
            return "resolved-text"

        out = resolve_sampling_in_orchestrator(
            {"messages": [{"role": "user", "content": "hi"}]},
            resolver=resolver,
        )
        assert out == "resolved-text"
        assert len(calls) == 1

    def test_default_resolver_refuses(self) -> None:
        with pytest.raises(RuntimeError, match="orchestrator"):
            resolve_sampling_in_orchestrator({"messages": []})


class TestCacheReference:
    def test_cache_directive_parses_ttl_and_scope(self) -> None:
        directive = CacheDirective.from_meta({"ttlMs": 5000, "cacheScope": "run"})
        assert directive.ttl_ms == 5000
        assert directive.cache_scope == "run"
        assert directive.is_cacheable

    def test_absent_directive_is_not_cacheable(self) -> None:
        directive = CacheDirective.from_meta({})
        assert not directive.is_cacheable
        assert directive.ttl_ms == 0

    def test_cache_hit_references_producing_run_content_hash(self) -> None:
        # AC5: a cache hit references the content hash of the producing run.
        ref = CacheReference.for_hit(
            content_hash="deadbeef" * 8,
            producing_run_head="cafef00d" * 8,
            cache_scope="run",
        )
        wire = ref.to_dict()
        assert wire["cache_hit"] is True
        assert wire["content_hash"] == "deadbeef" * 8
        assert wire["producing_run_head"] == "cafef00d" * 8


class TestCompatShim:
    def test_deprecated_capabilities_named(self) -> None:
        assert frozenset({"roots", "sampling", "logging"}) == DEPRECATED_CAPABILITIES

    def test_shim_active_within_window(self) -> None:
        assert compat_shim_active("sampling", months_since_deprecation=6)

    def test_shim_expires_after_twelve_months(self) -> None:
        assert not compat_shim_active("sampling", months_since_deprecation=12)
        assert not compat_shim_active("sampling", months_since_deprecation=13)

    def test_non_deprecated_capability_is_never_shimmed(self) -> None:
        assert not compat_shim_active("tools", months_since_deprecation=1)


class TestStatelessCallRecord:
    def test_record_projects_meta_span_and_cache(self) -> None:
        meta = build_request_meta(
            method="tools/call",
            params_content_hash="a" * 64,
            run_root_hash="b" * 64,
            call_index=2,
            client_capabilities=_client_caps(),
        )
        rec = StatelessCallRecord.from_meta(
            method="tools/call",
            call_index=2,
            meta=meta,
            cache=CacheReference.for_hit(
                content_hash="1" * 64,
                producing_run_head="2" * 64,
                cache_scope="run",
            ),
        )
        payload = rec.to_journal_payload()
        assert payload["method"] == "tools/call"
        assert payload["call_index"] == 2
        assert payload["span_id"] == meta["traceparent"].split("-")[2]
        assert payload["trace_id"] == meta["traceparent"].split("-")[1]
        assert payload["cache"]["cache_hit"] is True
        # No session id leaks into the journal payload.
        assert "session_id" not in payload
