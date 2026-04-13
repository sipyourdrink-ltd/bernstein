"""Tests for ORCH-008: Structured error context in log messages."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from bernstein.core.log_context import (
    ErrorContext,
    LogContext,
    build_error_context,
    error_context,
    get_current_context,
    log_with_context,
)

# ---------------------------------------------------------------------------
# LogContext
# ---------------------------------------------------------------------------


class TestLogContext:
    """Tests for the LogContext dataclass."""

    def test_default_empty(self) -> None:
        ctx = LogContext()
        assert ctx.run_id == ""
        assert ctx.tick_number == 0
        assert ctx.task_id == ""
        assert ctx.session_id == ""

    def test_all_fields(self) -> None:
        ctx = LogContext(
            run_id="run-123",
            tick_number=42,
            task_id="T-001",
            session_id="sess-abc",
            component="orchestrator",
            operation="spawn",
        )
        assert ctx.run_id == "run-123"
        assert ctx.tick_number == 42
        assert ctx.task_id == "T-001"

    def test_with_task(self) -> None:
        ctx = LogContext(run_id="r1", tick_number=5)
        ctx2 = ctx.with_task("T-002")
        assert ctx2.task_id == "T-002"
        assert ctx2.run_id == "r1"
        assert ctx2.tick_number == 5
        # Original unchanged
        assert ctx.task_id == ""

    def test_with_session(self) -> None:
        ctx = LogContext(run_id="r1")
        ctx2 = ctx.with_session("sess-xyz")
        assert ctx2.session_id == "sess-xyz"
        assert ctx.session_id == ""

    def test_with_operation(self) -> None:
        ctx = LogContext(run_id="r1")
        ctx2 = ctx.with_operation("spawn")
        assert ctx2.operation == "spawn"

    def test_to_dict_excludes_empty(self) -> None:
        ctx = LogContext(run_id="r1", task_id="T-001")
        d = ctx.to_dict()
        assert "run_id" in d
        assert "task_id" in d
        assert "session_id" not in d
        assert "tick_number" not in d

    def test_format_prefix(self) -> None:
        ctx = LogContext(run_id="r1", tick_number=42, task_id="T-001")
        prefix = ctx.format_prefix()
        assert "run=r1" in prefix
        assert "tick=42" in prefix
        assert "task=T-001" in prefix

    def test_format_prefix_empty(self) -> None:
        ctx = LogContext()
        assert ctx.format_prefix() == ""

    def test_frozen(self) -> None:
        ctx = LogContext(run_id="r1")
        with pytest.raises(AttributeError):
            ctx.run_id = "r2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ErrorContext
# ---------------------------------------------------------------------------


class TestErrorContext:
    """Tests for the ErrorContext dataclass."""

    def test_to_dict(self) -> None:
        ctx = LogContext(run_id="r1", task_id="T-001")
        error_ctx = ErrorContext(
            context=ctx,
            error_type="RuntimeError",
            error_message="something failed",
            timestamp=1234567890.0,
        )
        d = error_ctx.to_dict()
        assert d["run_id"] == "r1"
        assert d["task_id"] == "T-001"
        assert d["error_type"] == "RuntimeError"
        assert d["error_message"] == "something failed"
        assert d["error_timestamp"] == pytest.approx(1234567890.0)


# ---------------------------------------------------------------------------
# build_error_context
# ---------------------------------------------------------------------------


class TestBuildErrorContext:
    """Tests for building error contexts from exceptions."""

    def test_basic_build(self) -> None:
        ctx = LogContext(run_id="r1", tick_number=5)
        exc = ValueError("bad value")
        error_ctx = build_error_context(ctx, exc)
        assert error_ctx.error_type == "ValueError"
        assert error_ctx.error_message == "bad value"
        assert error_ctx.context is ctx
        assert error_ctx.timestamp > 0

    def test_with_traceback(self) -> None:
        ctx = LogContext(run_id="r1")
        try:
            raise RuntimeError("test error")
        except RuntimeError as exc:
            error_ctx = build_error_context(ctx, exc, include_traceback=True)
        assert "RuntimeError" in error_ctx.traceback_str
        assert "test error" in error_ctx.traceback_str

    def test_without_traceback(self) -> None:
        ctx = LogContext(run_id="r1")
        exc = RuntimeError("test")
        error_ctx = build_error_context(ctx, exc, include_traceback=False)
        assert error_ctx.traceback_str == ""

    def test_truncated_traceback(self) -> None:
        ctx = LogContext()

        def deep_call(n: int) -> None:
            if n <= 0:
                raise RuntimeError("deep error")
            deep_call(n - 1)

        try:
            deep_call(50)
        except RuntimeError as exc:
            error_ctx = build_error_context(ctx, exc, max_traceback_lines=5)
        assert "truncated" in error_ctx.traceback_str


# ---------------------------------------------------------------------------
# log_with_context
# ---------------------------------------------------------------------------


class TestLogWithContext:
    """Tests for the log_with_context function."""

    def test_logs_with_prefix(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)
        ctx = LogContext(run_id="r1", tick_number=3)
        log_with_context(mock_logger, "info", "tick started", ctx)
        mock_logger.info.assert_called_once()
        args = mock_logger.info.call_args
        assert "run=r1" in args[0][0]
        assert "tick=3" in args[0][0]

    def test_logs_with_exception(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)
        ctx = LogContext(run_id="r1")
        exc = RuntimeError("boom")
        log_with_context(mock_logger, "error", "spawn failed", ctx, exc=exc)
        mock_logger.error.assert_called_once()
        call_kwargs = mock_logger.error.call_args[1]
        assert call_kwargs["exc_info"] is exc

    def test_logs_warning_level(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)
        ctx = LogContext()
        log_with_context(mock_logger, "warning", "something off", ctx)
        mock_logger.warning.assert_called_once()

    def test_extra_fields_attached(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)
        ctx = LogContext(run_id="r1")
        log_with_context(
            mock_logger,
            "info",
            "test",
            ctx,
            extra_fields={"custom_key": "custom_val"},
        )
        call_kwargs = mock_logger.info.call_args[1]
        assert "custom_key" in call_kwargs["extra"]

    def test_uses_current_context_when_none(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)
        ctx = LogContext(run_id="ctx-from-var", tick_number=99)
        with error_context(ctx):
            log_with_context(mock_logger, "info", "test message")
        args = mock_logger.info.call_args
        assert "ctx-from-var" in args[0][0]


# ---------------------------------------------------------------------------
# error_context context manager
# ---------------------------------------------------------------------------


class TestErrorContextManager:
    """Tests for the error_context context manager."""

    def test_sets_current_context(self) -> None:
        ctx = LogContext(run_id="r1", tick_number=42)
        assert get_current_context() is None
        with error_context(ctx) as active:
            assert active is ctx
            assert get_current_context() is ctx
        assert get_current_context() is None

    def test_nested_contexts(self) -> None:
        ctx1 = LogContext(run_id="outer")
        ctx2 = LogContext(run_id="inner")
        with error_context(ctx1):
            assert get_current_context() is ctx1
            with error_context(ctx2):
                assert get_current_context() is ctx2
            assert get_current_context() is ctx1
        assert get_current_context() is None

    def test_restores_on_exception(self) -> None:
        ctx = LogContext(run_id="r1")
        with pytest.raises(RuntimeError):
            with error_context(ctx):
                assert get_current_context() is ctx
                raise RuntimeError("test")
        assert get_current_context() is None


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    """Tests for frozen dataclass behavior."""

    def test_log_context_frozen(self) -> None:
        ctx = LogContext(run_id="r1")
        with pytest.raises(AttributeError):
            ctx.run_id = "r2"  # type: ignore[misc]

    def test_error_context_frozen(self) -> None:
        error_ctx = ErrorContext(
            context=LogContext(),
            error_type="E",
            error_message="m",
        )
        with pytest.raises(AttributeError):
            error_ctx.error_type = "F"  # type: ignore[misc]
