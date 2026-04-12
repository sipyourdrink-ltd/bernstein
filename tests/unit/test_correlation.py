"""Tests for correlation ID tracking."""

from __future__ import annotations

import logging

import pytest

from bernstein.core.observability.correlation import (
    CorrelationContext,
    CorrelationFilter,
    create_context,
    generate_correlation_id,
    get_current_context,
    get_current_correlation_id,
    set_correlation_id,
    set_current_context,
)


# --- CorrelationContext tests ---


class TestCorrelationContext:
    """Tests for CorrelationContext dataclass."""

    def test_defaults(self) -> None:
        ctx = CorrelationContext(correlation_id="abc", task_id="T-1")
        assert ctx.agent_id is None
        assert ctx.gate_name is None
        assert ctx.stage == "task"
        assert ctx.metadata == {}

    def test_to_dict(self) -> None:
        ctx = CorrelationContext(
            correlation_id="abc123",
            task_id="T-1",
            agent_id="agent-42",
            gate_name="lint",
            stage="gate",
            metadata={"key": "value"},
        )
        d = ctx.to_dict()
        assert d["correlation_id"] == "abc123"
        assert d["task_id"] == "T-1"
        assert d["agent_id"] == "agent-42"
        assert d["gate_name"] == "lint"
        assert d["stage"] == "gate"
        assert d["metadata"] == {"key": "value"}

    def test_with_agent(self) -> None:
        ctx = CorrelationContext(correlation_id="abc", task_id="T-1")
        agent_ctx = ctx.with_agent("agent-99")
        assert agent_ctx.agent_id == "agent-99"
        assert agent_ctx.stage == "agent"
        assert agent_ctx.correlation_id == "abc"
        assert agent_ctx.task_id == "T-1"

    def test_with_gate(self) -> None:
        ctx = CorrelationContext(correlation_id="abc", task_id="T-1", agent_id="a1")
        gate_ctx = ctx.with_gate("ruff-check")
        assert gate_ctx.gate_name == "ruff-check"
        assert gate_ctx.stage == "gate"
        assert gate_ctx.agent_id == "a1"

    def test_with_merge(self) -> None:
        ctx = CorrelationContext(correlation_id="abc", task_id="T-1")
        merge_ctx = ctx.with_merge()
        assert merge_ctx.stage == "merge"
        assert merge_ctx.correlation_id == "abc"

    def test_metadata_is_copied(self) -> None:
        ctx = CorrelationContext(correlation_id="abc", task_id="T-1", metadata={"x": 1})
        agent_ctx = ctx.with_agent("a1")
        agent_ctx.metadata["y"] = 2
        assert "y" not in ctx.metadata


# --- generate_correlation_id ---


class TestGenerateCorrelationId:
    """Tests for generate_correlation_id()."""

    def test_returns_string(self) -> None:
        cid = generate_correlation_id()
        assert isinstance(cid, str)

    def test_length(self) -> None:
        cid = generate_correlation_id()
        assert len(cid) == 12

    def test_unique(self) -> None:
        ids = {generate_correlation_id() for _ in range(100)}
        assert len(ids) == 100


# --- Context management ---


class TestContextManagement:
    """Tests for context get/set functions."""

    def test_create_context_sets_current(self) -> None:
        ctx = create_context("T-42")
        assert ctx.task_id == "T-42"
        assert ctx.correlation_id != ""

        current = get_current_context()
        assert current is not None
        assert current.task_id == "T-42"

    def test_get_current_correlation_id(self) -> None:
        ctx = create_context("T-99")
        cid = get_current_correlation_id()
        assert cid == ctx.correlation_id

    def test_set_correlation_id_creates_context(self) -> None:
        # Reset context first
        set_current_context(None)  # type: ignore[arg-type]
        set_correlation_id("manual-id")
        ctx = get_current_context()
        assert ctx is not None
        assert ctx.correlation_id == "manual-id"

    def test_set_current_context(self) -> None:
        custom = CorrelationContext(correlation_id="custom", task_id="T-X")
        set_current_context(custom)
        assert get_current_context() is custom


# --- CorrelationFilter tests ---


class TestCorrelationFilter:
    """Tests for the logging CorrelationFilter."""

    def test_adds_correlation_id_to_record(self) -> None:
        create_context("T-filter-test")
        f = CorrelationFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        result = f.filter(record)
        assert result is True
        assert hasattr(record, "correlation_id")
        assert record.correlation_id != "none"  # type: ignore[attr-defined]
        assert hasattr(record, "task_id")
        assert record.task_id == "T-filter-test"  # type: ignore[attr-defined]

    def test_defaults_to_none_without_context(self) -> None:
        set_current_context(None)  # type: ignore[arg-type]
        f = CorrelationFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        f.filter(record)
        assert record.correlation_id == "none"  # type: ignore[attr-defined]
        assert record.task_id == "none"  # type: ignore[attr-defined]
        assert record.agent_id == "none"  # type: ignore[attr-defined]
