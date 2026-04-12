"""Backward-compat shim — re-exports from bernstein.core.observability.tool_timing."""

from bernstein.core.observability.tool_timing import (
    ToolTimingRecord,
    ToolTimingRecorder,
    get_recorder,
    logger,
    reset_recorder,
)

__all__ = [
    "ToolTimingRecord",
    "ToolTimingRecorder",
    "get_recorder",
    "logger",
    "reset_recorder",
]
