"""Tests for context-window utilization helpers."""

from __future__ import annotations

from bernstein.core.context_window import (
    ContextWindowUtilization,
    compute_context_window_utilization,
)


def test_compute_context_window_utilization_returns_snapshot() -> None:
    utilization = compute_context_window_utilization(tokens_used=40_000, max_context_tokens=200_000)

    assert utilization == ContextWindowUtilization(
        tokens_used=40_000,
        max_context_tokens=200_000,
        utilization_pct=20.0,
        over_warning_threshold=False,
    )


def test_compute_context_window_utilization_returns_none_without_capacity() -> None:
    assert compute_context_window_utilization(tokens_used=10_000, max_context_tokens=0) is None


def test_compute_context_window_utilization_flags_warning_threshold() -> None:
    utilization = compute_context_window_utilization(tokens_used=160_000, max_context_tokens=200_000)

    assert utilization is not None
    assert utilization.utilization_pct == 80.0
    assert utilization.over_warning_threshold is True
