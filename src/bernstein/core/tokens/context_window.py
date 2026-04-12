"""Helpers for tracking context-window utilization per agent session."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CONTEXT_UTILIZATION_WARN_PCT = 80.0


@dataclass(frozen=True)
class ContextWindowUtilization:
    """Computed context-window utilization for an agent.

    Attributes:
        tokens_used: Total tokens consumed by the session so far.
        max_context_tokens: Maximum context window for the selected provider/model.
        utilization_pct: Percentage of the context window consumed (0-100+).
        over_warning_threshold: Whether utilization exceeds the warning threshold.
    """

    tokens_used: int
    max_context_tokens: int
    utilization_pct: float
    over_warning_threshold: bool


def compute_context_window_utilization(
    tokens_used: int,
    max_context_tokens: int,
    *,
    warning_threshold_pct: float = DEFAULT_CONTEXT_UTILIZATION_WARN_PCT,
) -> ContextWindowUtilization | None:
    """Compute context-window utilization from token usage and capacity.

    Args:
        tokens_used: Total tokens consumed by the session.
        max_context_tokens: Maximum supported context window.
        warning_threshold_pct: Warning threshold in percentage points.

    Returns:
        A utilization snapshot, or ``None`` when no meaningful capacity exists.
    """
    if max_context_tokens <= 0:
        return None
    safe_tokens_used = max(tokens_used, 0)
    utilization_pct = round((safe_tokens_used / max_context_tokens) * 100.0, 2)
    return ContextWindowUtilization(
        tokens_used=safe_tokens_used,
        max_context_tokens=max_context_tokens,
        utilization_pct=utilization_pct,
        over_warning_threshold=utilization_pct >= warning_threshold_pct,
    )
