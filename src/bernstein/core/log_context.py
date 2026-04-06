"""Structured error context for all orchestrator log messages.

Provides a ``LogContext`` dataclass that carries structured metadata
(run_id, tick_number, task_id, session_id) and can be attached to
exception handlers and log messages throughout the orchestrator.

The ``ErrorContext`` wrapper combines a ``LogContext`` with exception
details into a structured dict suitable for JSON logging.

Usage::

    ctx = LogContext(run_id="abc123", tick_number=42, task_id="T-001")
    with error_context(ctx):
        # exception handlers in this block automatically get structured context
        ...

    # Or manually:
    log_with_context(logger, "error", "Spawn failed", ctx, exc=some_error)
"""

from __future__ import annotations

import contextlib
import logging
import time
import traceback
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

# Context variable for the current log context (thread-safe via contextvars)
_current_context: ContextVar[LogContext | None] = ContextVar("_current_context", default=None)


@dataclass(frozen=True)
class LogContext:
    """Structured context for log messages and error reports.

    All fields are optional; callers populate only what is known at the
    call site.

    Attributes:
        run_id: Orchestrator run/session ID.
        tick_number: Current tick number.
        task_id: Task ID being processed.
        session_id: Agent session ID.
        component: Name of the component emitting the log.
        operation: Name of the operation being performed.
    """

    run_id: str = ""
    tick_number: int = 0
    task_id: str = ""
    session_id: str = ""
    component: str = ""
    operation: str = ""

    def with_task(self, task_id: str) -> LogContext:
        """Return a new context with the task_id set.

        Args:
            task_id: Task ID to set.

        Returns:
            New LogContext with updated task_id.
        """
        return LogContext(
            run_id=self.run_id,
            tick_number=self.tick_number,
            task_id=task_id,
            session_id=self.session_id,
            component=self.component,
            operation=self.operation,
        )

    def with_session(self, session_id: str) -> LogContext:
        """Return a new context with the session_id set.

        Args:
            session_id: Session ID to set.

        Returns:
            New LogContext with updated session_id.
        """
        return LogContext(
            run_id=self.run_id,
            tick_number=self.tick_number,
            task_id=self.task_id,
            session_id=session_id,
            component=self.component,
            operation=self.operation,
        )

    def with_operation(self, operation: str) -> LogContext:
        """Return a new context with the operation set.

        Args:
            operation: Operation name to set.

        Returns:
            New LogContext with updated operation.
        """
        return LogContext(
            run_id=self.run_id,
            tick_number=self.tick_number,
            task_id=self.task_id,
            session_id=self.session_id,
            component=self.component,
            operation=operation,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict, excluding empty fields.

        Returns:
            Dict with only non-empty fields.
        """
        return {k: v for k, v in asdict(self).items() if v}

    def format_prefix(self) -> str:
        """Format a human-readable prefix for log messages.

        Returns:
            String like ``[run=abc tick=42 task=T-001 session=xyz]``.
        """
        parts: list[str] = []
        if self.run_id:
            parts.append(f"run={self.run_id}")
        if self.tick_number:
            parts.append(f"tick={self.tick_number}")
        if self.task_id:
            parts.append(f"task={self.task_id}")
        if self.session_id:
            parts.append(f"session={self.session_id}")
        if self.operation:
            parts.append(f"op={self.operation}")
        return f"[{' '.join(parts)}]" if parts else ""


@dataclass(frozen=True)
class ErrorContext:
    """Structured error context combining LogContext with exception details.

    Attributes:
        context: The LogContext at the time of the error.
        error_type: Exception class name.
        error_message: Exception message.
        traceback_str: Formatted traceback string (truncated).
        timestamp: Unix timestamp of the error.
    """

    context: LogContext
    error_type: str
    error_message: str
    traceback_str: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to a flat dict for JSON logging.

        Returns:
            Dict combining context fields and error details.
        """
        result: dict[str, Any] = self.context.to_dict()
        result["error_type"] = self.error_type
        result["error_message"] = self.error_message
        if self.traceback_str:
            result["traceback"] = self.traceback_str
        if self.timestamp:
            result["error_timestamp"] = self.timestamp
        return result


def build_error_context(
    ctx: LogContext,
    exc: Exception,
    *,
    include_traceback: bool = True,
    max_traceback_lines: int = 20,
) -> ErrorContext:
    """Build a structured error context from a LogContext and exception.

    Args:
        ctx: The current log context.
        exc: The exception.
        include_traceback: Whether to include the traceback string.
        max_traceback_lines: Maximum traceback lines to include.

    Returns:
        An ErrorContext combining the log context with exception details.
    """
    tb_str = ""
    if include_traceback:
        tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        full_tb = "".join(tb_lines)
        lines = full_tb.splitlines()
        if len(lines) > max_traceback_lines:
            tb_str = "\n".join(lines[:max_traceback_lines]) + "\n... (truncated)"
        else:
            tb_str = full_tb

    return ErrorContext(
        context=ctx,
        error_type=type(exc).__name__,
        error_message=str(exc),
        traceback_str=tb_str,
        timestamp=time.time(),
    )


def log_with_context(
    target_logger: logging.Logger,
    level: str,
    message: str,
    ctx: LogContext | None = None,
    *,
    exc: Exception | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> None:
    """Log a message with structured context.

    Attaches the LogContext fields as ``extra`` data on the log record,
    which structured formatters (like JsonFormatter) can pick up.

    Args:
        target_logger: Logger to emit the message to.
        level: Log level name (e.g. "error", "warning", "info").
        message: The log message.
        ctx: Structured context (uses current context if None).
        exc: Optional exception to include.
        extra_fields: Additional fields to attach to the log record.
    """
    if ctx is None:
        ctx = get_current_context()

    extra: dict[str, Any] = {}
    if ctx is not None:
        prefix = ctx.format_prefix()
        if prefix:
            message = f"{prefix} {message}"
        extra.update(ctx.to_dict())

    if exc is not None:
        error_ctx = build_error_context(ctx or LogContext(), exc)
        extra["error_type"] = error_ctx.error_type
        extra["error_message"] = error_ctx.error_message

    if extra_fields:
        extra.update(extra_fields)

    log_func = getattr(target_logger, level.lower(), target_logger.info)
    if exc is not None:
        log_func(message, extra=extra, exc_info=exc)
    else:
        log_func(message, extra=extra)


@contextlib.contextmanager
def error_context(ctx: LogContext) -> Generator[LogContext, None, None]:
    """Context manager that sets the current LogContext.

    Within this block, ``get_current_context()`` returns the given context,
    and ``log_with_context()`` calls without an explicit context will use it.

    Args:
        ctx: The LogContext to make current.

    Yields:
        The active LogContext.
    """
    token = _current_context.set(ctx)
    try:
        yield ctx
    finally:
        _current_context.reset(token)


def get_current_context() -> LogContext | None:
    """Return the current LogContext from the context variable.

    Returns:
        The active LogContext, or None if none is set.
    """
    return _current_context.get()
