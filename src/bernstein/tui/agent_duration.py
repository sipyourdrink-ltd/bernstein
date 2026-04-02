"""Agent duration display for TUI agent panel."""

from __future__ import annotations

import time
from datetime import timedelta


def format_agent_duration(start_time: float) -> str:
    """Format agent uptime duration.

    Args:
        start_time: Agent start timestamp.

    Returns:
        Formatted duration string (e.g., "2m 34s" or "1h 07m").
    """
    elapsed = time.time() - start_time
    timedelta(seconds=int(elapsed))

    hours, remainder = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    else:
        return f"{minutes}m {seconds:02d}s"


def get_duration_color(start_time: float) -> str:
    """Get color for agent duration based on uptime.

    Args:
        start_time: Agent start timestamp.

    Returns:
        Rich color string: green (< 10 min), yellow (10-30 min), red (> 30 min).
    """
    elapsed = time.time() - start_time
    minutes = elapsed / 60

    if minutes < 10:
        return "green"
    elif minutes < 30:
        return "yellow"
    else:
        return "red"
