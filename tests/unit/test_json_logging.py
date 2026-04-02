"""Tests for JSON structured logging."""

from __future__ import annotations

import json
import logging
from io import StringIO

from bernstein.core.correlation import set_current_context
from bernstein.core.json_logging import JsonFormatter


def test_json_formatter_basic() -> None:
    """Test basic JSON formatting."""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test.component",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Test message",
        args=(),
        exc_info=None,
    )

    output = formatter.format(record)
    data = json.loads(output)

    assert data["level"] == "INFO"
    assert data["component"] == "test.component"
    assert data["message"] == "Test message"
    assert data["task_id"] == "none"
    assert data["agent_id"] == "none"
    assert data["correlation_id"] == "none"
    assert "timestamp" in data


def test_json_formatter_with_context() -> None:
    """Test JSON formatting with correlation context."""
    formatter = JsonFormatter()

    # Manually add attributes that CorrelationFilter would add
    record = logging.LogRecord(
        name="test.component",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Task started",
        args=(),
        exc_info=None,
    )
    record.task_id = "task-123"
    record.agent_id = "agent-456"
    record.correlation_id = "corr-789"

    output = formatter.format(record)
    data = json.loads(output)

    assert data["task_id"] == "task-123"
    assert data["agent_id"] == "agent-456"
    assert data["correlation_id"] == "corr-789"


def test_json_formatter_with_extra() -> None:
    """Test JSON formatting with extra fields."""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Msg",
        args=(),
        exc_info=None,
    )
    record.custom_field = "custom-value"

    output = formatter.format(record)
    data = json.loads(output)

    assert data["custom_field"] == "custom-value"


def test_json_formatter_with_exception() -> None:
    """Test JSON formatting with exception info."""
    formatter = JsonFormatter()
    try:
        raise ValueError("Oops")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=10,
        msg="Error occurred",
        args=(),
        exc_info=exc_info,
    )

    output = formatter.format(record)
    data = json.loads(output)

    assert "exception" in data
    assert "ValueError: Oops" in data["exception"]


def test_json_logging_end_to_end() -> None:
    """Test full integration of JsonFormatter and CorrelationFilter."""
    from bernstein.core.correlation import CorrelationFilter, create_context

    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationFilter())

    test_logger = logging.getLogger("test_json_e2e")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    try:
        # Without context
        test_logger.info("No context")
        data1 = json.loads(stream.getvalue().splitlines()[-1])
        assert data1["task_id"] == "none"
        assert data1["message"] == "No context"

        # With context
        ctx = create_context("task-999")
        ctx = ctx.with_agent("agent-888")
        set_current_context(ctx)

        test_logger.info("With context")
        data2 = json.loads(stream.getvalue().splitlines()[-1])
        assert data2["task_id"] == "task-999"
        assert data2["agent_id"] == "agent-888"
        assert data2["message"] == "With context"

    finally:
        test_logger.removeHandler(handler)
