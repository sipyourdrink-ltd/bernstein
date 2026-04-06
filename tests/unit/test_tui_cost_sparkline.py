"""Tests for TUI-006: Cost sparkline in TUI sidebar."""
# pyright: reportUnknownMemberType=false

from __future__ import annotations

import pytest

from bernstein.tui.cost_sparkline import (
    CostTracker,
    render_cost_sidebar,
    render_cost_sparkline,
    render_cost_sparkline_rich,
)


class TestCostTracker:
    def test_add_sample(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(1.50, timestamp=1000.0)
        assert len(tracker.samples) == 1
        assert tracker.samples[0].cumulative_usd == 1.50

    def test_latest_cost(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(1.0, timestamp=1000.0)
        tracker.add_sample(2.5, timestamp=1001.0)
        assert tracker.latest_cost == 2.5

    def test_latest_cost_empty(self) -> None:
        tracker = CostTracker()
        assert tracker.latest_cost == 0.0

    def test_delta_series(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(0.0, timestamp=1000.0)
        tracker.add_sample(1.0, timestamp=1001.0)
        tracker.add_sample(1.5, timestamp=1002.0)
        tracker.add_sample(3.0, timestamp=1003.0)
        deltas = tracker.delta_series()
        assert deltas == [1.0, 0.5, 1.5]

    def test_delta_series_insufficient(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(1.0, timestamp=1000.0)
        assert tracker.delta_series() == []

    def test_cumulative_series(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(1.0, timestamp=1000.0)
        tracker.add_sample(2.5, timestamp=1001.0)
        assert tracker.cumulative_series() == [1.0, 2.5]

    def test_spend_rate(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(0.0, timestamp=1000.0)
        tracker.add_sample(6.0, timestamp=1060.0)  # $6 in 1 minute
        assert tracker.total_spend_rate == pytest.approx(6.0)

    def test_spend_rate_insufficient(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(5.0, timestamp=1000.0)
        assert tracker.total_spend_rate == 0.0

    def test_ring_buffer_capped(self) -> None:
        tracker = CostTracker(max_samples=5)
        for i in range(10):
            tracker.add_sample(float(i), timestamp=1000.0 + i)
        assert len(tracker.samples) == 5
        assert tracker.samples[0].cumulative_usd == 5.0

    def test_negative_deltas_clamped(self) -> None:
        """Negative cost deltas should be clamped to 0."""
        tracker = CostTracker()
        tracker.add_sample(5.0, timestamp=1000.0)
        tracker.add_sample(3.0, timestamp=1001.0)  # Decrease (shouldn't happen but handle it)
        deltas = tracker.delta_series()
        assert deltas == [0.0]


class TestRenderCostSparkline:
    def test_empty_returns_empty(self) -> None:
        assert render_cost_sparkline([]) == ""

    def test_all_zeros(self) -> None:
        result = render_cost_sparkline([0.0, 0.0, 0.0], width=3)
        assert len(result) == 3

    def test_increasing_values(self) -> None:
        result = render_cost_sparkline([1.0, 2.0, 3.0, 4.0], width=4)
        assert len(result) == 4
        # Last char should be highest
        assert result[-1] >= result[0]

    def test_width_limits_output(self) -> None:
        result = render_cost_sparkline([1.0] * 20, width=5)
        assert len(result) == 5


class TestRenderCostSparklineRich:
    def test_empty_tracker(self) -> None:
        tracker = CostTracker()
        text = render_cost_sparkline_rich(tracker)
        assert "$0.00" in text.plain

    def test_with_data(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(0.0, timestamp=1000.0)
        tracker.add_sample(1.0, timestamp=1001.0)
        tracker.add_sample(2.0, timestamp=1002.0)
        text = render_cost_sparkline_rich(tracker)
        assert "$" in text.plain

    def test_with_rate(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(0.0, timestamp=1000.0)
        tracker.add_sample(3.0, timestamp=1060.0)
        text = render_cost_sparkline_rich(tracker, show_rate=True)
        assert "/min" in text.plain


class TestRenderCostSidebar:
    def test_no_budget(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(5.0, timestamp=1000.0)
        text = render_cost_sidebar(tracker)
        assert "Cost" in text.plain

    def test_with_budget(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(8.0, timestamp=1000.0)
        text = render_cost_sidebar(tracker, budget_usd=10.0)
        assert "Budget" in text.plain
        assert "remaining" in text.plain

    def test_budget_zero(self) -> None:
        tracker = CostTracker()
        tracker.add_sample(5.0, timestamp=1000.0)
        text = render_cost_sidebar(tracker, budget_usd=0.0)
        # Should not crash, no budget line shown
        assert "Budget" not in text.plain
