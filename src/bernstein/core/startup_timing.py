"""Opt-in process timestamps for CLI startup diagnostics."""

from __future__ import annotations

import os
import sys
import time


def mark_startup_phase(phase: str) -> None:
    """Write a monotonic timestamp to stderr when the operator requests a trace."""
    if os.environ.get("BERNSTEIN_STARTUP_TIMINGS") == "1":
        print(f"bernstein-startup {phase} {time.perf_counter_ns()}", file=sys.stderr, flush=True)
