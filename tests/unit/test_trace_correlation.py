"""Tests for W3C trace context correlation."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from bernstein.core.trace_correlation import (
    CorrelationRecord,
    TraceContext,
    build_correlation_env,
    create_correlation_record,
    format_traceparent,
    generate_trace_context,
    parse_traceparent,
    save_correlation,
)

# ---------------------------------------------------------------------------
# TraceContext
# ---------------------------------------------------------------------------


class TestTraceContext:
    """TraceContext dataclass basics."""

    def test_field_lengths(self) -> None:
        ctx = TraceContext(trace_id="a" * 32, span_id="b" * 16, trace_flags="01")
        assert len(ctx.trace_id) == 32
        assert len(ctx.span_id) == 16
        assert len(ctx.trace_flags) == 2

    def test_frozen(self) -> None:
        ctx = TraceContext(trace_id="a" * 32, span_id="b" * 16)
        with pytest.raises(FrozenInstanceError):
            ctx.trace_id = "x" * 32  # type: ignore[misc]

    def test_default_flags(self) -> None:
        ctx = TraceContext(trace_id="a" * 32, span_id="b" * 16)
        assert ctx.trace_flags == "01"


# ---------------------------------------------------------------------------
# generate_trace_context
# ---------------------------------------------------------------------------


class TestGenerateTraceContext:
    """generate_trace_context produces valid hex strings."""

    def test_lengths(self) -> None:
        ctx = generate_trace_context()
        assert len(ctx.trace_id) == 32
        assert len(ctx.span_id) == 16
        assert len(ctx.trace_flags) == 2

    def test_hex_chars(self) -> None:
        ctx = generate_trace_context()
        int(ctx.trace_id, 16)  # raises ValueError if not hex
        int(ctx.span_id, 16)

    def test_unique(self) -> None:
        a = generate_trace_context()
        b = generate_trace_context()
        assert a.trace_id != b.trace_id
        assert a.span_id != b.span_id

    def test_sampled_flag(self) -> None:
        ctx = generate_trace_context()
        assert ctx.trace_flags == "01"


# ---------------------------------------------------------------------------
# format / parse roundtrip
# ---------------------------------------------------------------------------


class TestFormatParse:
    """format_traceparent and parse_traceparent roundtrip."""

    def test_roundtrip(self) -> None:
        ctx = generate_trace_context()
        header = format_traceparent(ctx)
        parsed = parse_traceparent(header)
        assert parsed is not None
        assert parsed.trace_id == ctx.trace_id
        assert parsed.span_id == ctx.span_id
        assert parsed.trace_flags == ctx.trace_flags

    def test_format_structure(self) -> None:
        ctx = TraceContext(trace_id="a" * 32, span_id="b" * 16, trace_flags="01")
        header = format_traceparent(ctx)
        assert header == f"00-{'a' * 32}-{'b' * 16}-01"

    def test_parse_spec_example(self) -> None:
        """Parse the example from the W3C spec."""
        header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        ctx = parse_traceparent(header)
        assert ctx is not None
        assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert ctx.span_id == "00f067aa0ba902b7"
        assert ctx.trace_flags == "01"


# ---------------------------------------------------------------------------
# parse_traceparent — invalid inputs
# ---------------------------------------------------------------------------


class TestParseInvalid:
    """parse_traceparent returns None for invalid inputs."""

    def test_empty(self) -> None:
        assert parse_traceparent("") is None

    def test_garbage(self) -> None:
        assert parse_traceparent("not-a-traceparent") is None

    def test_wrong_version(self) -> None:
        assert parse_traceparent("ff-" + "a" * 32 + "-" + "b" * 16 + "-01") is None

    def test_short_trace_id(self) -> None:
        assert parse_traceparent("00-abc-" + "b" * 16 + "-01") is None

    def test_short_span_id(self) -> None:
        assert parse_traceparent("00-" + "a" * 32 + "-abc-01") is None

    def test_all_zero_trace_id(self) -> None:
        assert parse_traceparent("00-" + "0" * 32 + "-" + "b" * 16 + "-01") is None

    def test_all_zero_span_id(self) -> None:
        assert parse_traceparent("00-" + "a" * 32 + "-" + "0" * 16 + "-01") is None

    def test_uppercase_normalised(self) -> None:
        """Uppercase hex is normalised to lowercase and parsed correctly."""
        header = "00-" + "A" * 32 + "-" + "B" * 16 + "-01"
        ctx = parse_traceparent(header)
        assert ctx is not None
        assert ctx.trace_id == "a" * 32


# ---------------------------------------------------------------------------
# build_correlation_env
# ---------------------------------------------------------------------------


class TestBuildCorrelationEnv:
    """build_correlation_env includes all required keys."""

    def test_keys_present(self) -> None:
        ctx = generate_trace_context()
        env = build_correlation_env(ctx)
        assert "TRACEPARENT" in env
        assert "BERNSTEIN_TRACE_ID" in env
        assert "BERNSTEIN_SPAN_ID" in env

    def test_values(self) -> None:
        ctx = generate_trace_context()
        env = build_correlation_env(ctx)
        assert env["BERNSTEIN_TRACE_ID"] == ctx.trace_id
        assert env["BERNSTEIN_SPAN_ID"] == ctx.span_id
        assert env["TRACEPARENT"] == format_traceparent(ctx)

    def test_all_values_are_strings(self) -> None:
        ctx = generate_trace_context()
        env = build_correlation_env(ctx)
        for key, val in env.items():
            assert isinstance(key, str)
            assert isinstance(val, str)


# ---------------------------------------------------------------------------
# save_correlation
# ---------------------------------------------------------------------------


class TestSaveCorrelation:
    """save_correlation writes JSONL."""

    def test_writes_jsonl(self, tmp_path: Path) -> None:
        record = CorrelationRecord(
            bernstein_trace_id="a" * 32,
            agent_session_id="session-1",
            task_id="task-42",
            traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
            created_at="2026-04-10T00:00:00+00:00",
        )
        path = save_correlation(record, tmp_path)
        assert path.exists()
        assert path.name == "trace_correlations.jsonl"

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["bernstein_trace_id"] == "a" * 32
        assert data["task_id"] == "task-42"

    def test_appends(self, tmp_path: Path) -> None:
        for i in range(3):
            record = CorrelationRecord(
                bernstein_trace_id=f"{i:032x}",
                agent_session_id=f"session-{i}",
                task_id=f"task-{i}",
                traceparent=f"00-{i:032x}-{'b' * 16}-01",
                created_at="2026-04-10T00:00:00+00:00",
            )
            save_correlation(record, tmp_path)

        path = tmp_path / "trace_correlations.jsonl"
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_creates_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "sub" / "dir"
        record = CorrelationRecord(
            bernstein_trace_id="c" * 32,
            agent_session_id="s",
            task_id="t",
            traceparent="00-" + "c" * 32 + "-" + "d" * 16 + "-01",
            created_at="2026-04-10T00:00:00+00:00",
        )
        path = save_correlation(record, nested)
        assert path.exists()

    def test_frozen(self) -> None:
        record = CorrelationRecord(
            bernstein_trace_id="a" * 32,
            agent_session_id="s",
            task_id="t",
            traceparent="tp",
            created_at="now",
        )
        with pytest.raises(FrozenInstanceError):
            record.task_id = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# create_correlation_record convenience factory
# ---------------------------------------------------------------------------


class TestCreateCorrelationRecord:
    """create_correlation_record populates all fields."""

    def test_fields(self) -> None:
        ctx = generate_trace_context()
        rec = create_correlation_record(ctx, agent_session_id="s-1", task_id="t-1")
        assert rec.bernstein_trace_id == ctx.trace_id
        assert rec.agent_session_id == "s-1"
        assert rec.task_id == "t-1"
        assert rec.traceparent == format_traceparent(ctx)
        assert rec.created_at  # non-empty ISO timestamp
