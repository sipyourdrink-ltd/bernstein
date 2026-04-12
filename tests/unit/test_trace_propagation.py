"""Tests for distributed tracing context propagation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from bernstein.core.observability.trace_propagation import (
    TraceContext,
    child_span,
    extract_context,
    from_traceparent,
    generate_span_id,
    generate_trace_id,
    inject_headers,
    new_trace,
    to_traceparent,
)

# ---------------------------------------------------------------------------
# TraceContext dataclass
# ---------------------------------------------------------------------------


class TestTraceContext:
    """TraceContext dataclass basics."""

    def test_field_values(self) -> None:
        ctx = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id="c" * 16,
            sampled=True,
        )
        assert ctx.trace_id == "a" * 32
        assert ctx.span_id == "b" * 16
        assert ctx.parent_span_id == "c" * 16
        assert ctx.sampled is True

    def test_frozen_trace_id(self) -> None:
        ctx = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            sampled=True,
        )
        with pytest.raises(FrozenInstanceError):
            ctx.trace_id = "x" * 32  # type: ignore[misc]

    def test_frozen_span_id(self) -> None:
        ctx = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            sampled=True,
        )
        with pytest.raises(FrozenInstanceError):
            ctx.span_id = "x" * 16  # type: ignore[misc]

    def test_frozen_sampled(self) -> None:
        ctx = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            sampled=True,
        )
        with pytest.raises(FrozenInstanceError):
            ctx.sampled = False  # type: ignore[misc]

    def test_parent_span_id_none_for_root(self) -> None:
        ctx = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            sampled=True,
        )
        assert ctx.parent_span_id is None

    def test_equality(self) -> None:
        ctx1 = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            sampled=True,
        )
        ctx2 = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            sampled=True,
        )
        assert ctx1 == ctx2


# ---------------------------------------------------------------------------
# generate_trace_id
# ---------------------------------------------------------------------------


class TestGenerateTraceId:
    """generate_trace_id produces valid 32-hex-char strings."""

    def test_length(self) -> None:
        tid = generate_trace_id()
        assert len(tid) == 32

    def test_hex_chars(self) -> None:
        tid = generate_trace_id()
        int(tid, 16)  # raises ValueError if not valid hex

    def test_unique(self) -> None:
        ids = {generate_trace_id() for _ in range(50)}
        assert len(ids) == 50


# ---------------------------------------------------------------------------
# generate_span_id
# ---------------------------------------------------------------------------


class TestGenerateSpanId:
    """generate_span_id produces valid 16-hex-char strings."""

    def test_length(self) -> None:
        sid = generate_span_id()
        assert len(sid) == 16

    def test_hex_chars(self) -> None:
        sid = generate_span_id()
        int(sid, 16)  # raises ValueError if not valid hex

    def test_unique(self) -> None:
        ids = {generate_span_id() for _ in range(50)}
        assert len(ids) == 50


# ---------------------------------------------------------------------------
# new_trace
# ---------------------------------------------------------------------------


class TestNewTrace:
    """new_trace creates a root TraceContext."""

    def test_root_has_no_parent(self) -> None:
        ctx = new_trace()
        assert ctx.parent_span_id is None

    def test_sampled_by_default(self) -> None:
        ctx = new_trace()
        assert ctx.sampled is True

    def test_sampled_false(self) -> None:
        ctx = new_trace(sampled=False)
        assert ctx.sampled is False

    def test_id_lengths(self) -> None:
        ctx = new_trace()
        assert len(ctx.trace_id) == 32
        assert len(ctx.span_id) == 16

    def test_unique_traces(self) -> None:
        a = new_trace()
        b = new_trace()
        assert a.trace_id != b.trace_id
        assert a.span_id != b.span_id


# ---------------------------------------------------------------------------
# child_span
# ---------------------------------------------------------------------------


class TestChildSpan:
    """child_span creates a child context from a parent."""

    def test_inherits_trace_id(self) -> None:
        parent = new_trace()
        child = child_span(parent)
        assert child.trace_id == parent.trace_id

    def test_new_span_id(self) -> None:
        parent = new_trace()
        child = child_span(parent)
        assert child.span_id != parent.span_id

    def test_parent_span_id_set(self) -> None:
        parent = new_trace()
        child = child_span(parent)
        assert child.parent_span_id == parent.span_id

    def test_inherits_sampled_true(self) -> None:
        parent = new_trace(sampled=True)
        child = child_span(parent)
        assert child.sampled is True

    def test_inherits_sampled_false(self) -> None:
        parent = new_trace(sampled=False)
        child = child_span(parent)
        assert child.sampled is False

    def test_grandchild(self) -> None:
        root = new_trace()
        child = child_span(root)
        grandchild = child_span(child)
        assert grandchild.trace_id == root.trace_id
        assert grandchild.parent_span_id == child.span_id
        assert grandchild.span_id != child.span_id


# ---------------------------------------------------------------------------
# to_traceparent
# ---------------------------------------------------------------------------


class TestToTraceparent:
    """to_traceparent formats W3C traceparent header."""

    def test_format_sampled(self) -> None:
        ctx = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            sampled=True,
        )
        header = to_traceparent(ctx)
        assert header == f"00-{'a' * 32}-{'b' * 16}-01"

    def test_format_not_sampled(self) -> None:
        ctx = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            sampled=False,
        )
        header = to_traceparent(ctx)
        assert header == f"00-{'a' * 32}-{'b' * 16}-00"

    def test_four_segments(self) -> None:
        ctx = new_trace()
        header = to_traceparent(ctx)
        parts = header.split("-")
        assert len(parts) == 4
        assert parts[0] == "00"
        assert len(parts[1]) == 32
        assert len(parts[2]) == 16
        assert len(parts[3]) == 2


# ---------------------------------------------------------------------------
# from_traceparent
# ---------------------------------------------------------------------------


class TestFromTraceparent:
    """from_traceparent parses W3C traceparent header."""

    def test_parse_spec_example(self) -> None:
        header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        ctx = from_traceparent(header)
        assert ctx is not None
        assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert ctx.span_id == "00f067aa0ba902b7"
        assert ctx.sampled is True

    def test_parse_not_sampled(self) -> None:
        header = f"00-{'a' * 32}-{'b' * 16}-00"
        ctx = from_traceparent(header)
        assert ctx is not None
        assert ctx.sampled is False

    def test_parent_span_id_none(self) -> None:
        header = f"00-{'a' * 32}-{'b' * 16}-01"
        ctx = from_traceparent(header)
        assert ctx is not None
        assert ctx.parent_span_id is None

    def test_roundtrip(self) -> None:
        original = new_trace()
        header = to_traceparent(original)
        parsed = from_traceparent(header)
        assert parsed is not None
        assert parsed.trace_id == original.trace_id
        assert parsed.span_id == original.span_id
        assert parsed.sampled == original.sampled

    def test_rejects_empty(self) -> None:
        assert from_traceparent("") is None

    def test_rejects_garbage(self) -> None:
        assert from_traceparent("not-a-valid-traceparent") is None

    def test_rejects_wrong_version(self) -> None:
        assert from_traceparent(f"ff-{'a' * 32}-{'b' * 16}-01") is None

    def test_rejects_short_trace_id(self) -> None:
        assert from_traceparent(f"00-abc-{'b' * 16}-01") is None

    def test_rejects_short_span_id(self) -> None:
        assert from_traceparent(f"00-{'a' * 32}-abc-01") is None

    def test_rejects_all_zero_trace_id(self) -> None:
        assert from_traceparent(f"00-{'0' * 32}-{'b' * 16}-01") is None

    def test_rejects_all_zero_span_id(self) -> None:
        assert from_traceparent(f"00-{'a' * 32}-{'0' * 16}-01") is None

    def test_normalises_uppercase(self) -> None:
        header = f"00-{'A' * 32}-{'B' * 16}-01"
        ctx = from_traceparent(header)
        assert ctx is not None
        assert ctx.trace_id == "a" * 32
        assert ctx.span_id == "b" * 16

    def test_strips_whitespace(self) -> None:
        header = f"  00-{'a' * 32}-{'b' * 16}-01  "
        ctx = from_traceparent(header)
        assert ctx is not None
        assert ctx.trace_id == "a" * 32


# ---------------------------------------------------------------------------
# inject_headers
# ---------------------------------------------------------------------------


class TestInjectHeaders:
    """inject_headers adds traceparent to a dict."""

    def test_adds_traceparent_key(self) -> None:
        ctx = new_trace()
        headers: dict[str, str] = {}
        inject_headers(ctx, headers)
        assert "traceparent" in headers

    def test_value_matches_to_traceparent(self) -> None:
        ctx = new_trace()
        headers: dict[str, str] = {}
        inject_headers(ctx, headers)
        assert headers["traceparent"] == to_traceparent(ctx)

    def test_preserves_existing_headers(self) -> None:
        ctx = new_trace()
        headers: dict[str, str] = {"Authorization": "Bearer token123"}
        inject_headers(ctx, headers)
        assert headers["Authorization"] == "Bearer token123"
        assert "traceparent" in headers


# ---------------------------------------------------------------------------
# extract_context
# ---------------------------------------------------------------------------


class TestExtractContext:
    """extract_context recovers TraceContext from headers."""

    def test_extract_valid(self) -> None:
        ctx = new_trace()
        headers: dict[str, str] = {}
        inject_headers(ctx, headers)
        recovered = extract_context(headers)
        assert recovered is not None
        assert recovered.trace_id == ctx.trace_id
        assert recovered.span_id == ctx.span_id
        assert recovered.sampled == ctx.sampled

    def test_case_insensitive_key(self) -> None:
        ctx = new_trace()
        headers = {"Traceparent": to_traceparent(ctx)}
        recovered = extract_context(headers)
        assert recovered is not None
        assert recovered.trace_id == ctx.trace_id

    def test_missing_header_returns_none(self) -> None:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        assert extract_context(headers) is None

    def test_empty_headers_returns_none(self) -> None:
        assert extract_context({}) is None

    def test_invalid_header_returns_none(self) -> None:
        headers = {"traceparent": "garbage"}
        assert extract_context(headers) is None


# ---------------------------------------------------------------------------
# End-to-end propagation
# ---------------------------------------------------------------------------


class TestEndToEndPropagation:
    """Full inject -> extract roundtrip across components."""

    def test_inject_extract_roundtrip(self) -> None:
        root = new_trace()
        child = child_span(root)
        headers: dict[str, str] = {}
        inject_headers(child, headers)
        recovered = extract_context(headers)
        assert recovered is not None
        assert recovered.trace_id == root.trace_id
        assert recovered.span_id == child.span_id

    def test_propagation_chain(self) -> None:
        """Simulate three-hop propagation: server -> spawner -> agent."""
        server_ctx = new_trace()

        # Server injects into request to spawner.
        spawner_headers: dict[str, str] = {}
        inject_headers(server_ctx, spawner_headers)

        # Spawner extracts, creates child, injects into agent.
        spawner_ctx = extract_context(spawner_headers)
        assert spawner_ctx is not None
        agent_parent = child_span(spawner_ctx)
        agent_headers: dict[str, str] = {}
        inject_headers(agent_parent, agent_headers)

        # Agent extracts — trace_id propagated end-to-end.
        agent_ctx = extract_context(agent_headers)
        assert agent_ctx is not None
        assert agent_ctx.trace_id == server_ctx.trace_id
        assert agent_ctx.span_id == agent_parent.span_id
