"""Backward-compat shim — re-exports from bernstein.core.observability.profiler."""

from bernstein.core.observability.profiler import (
    OrchestratorProfiler,
    ProfileResult,
    ProfilerSession,
    logger,
    resolve_profile_output_dir,
)

__all__ = [
    "OrchestratorProfiler",
    "ProfileResult",
    "ProfilerSession",
    "logger",
    "resolve_profile_output_dir",
]
