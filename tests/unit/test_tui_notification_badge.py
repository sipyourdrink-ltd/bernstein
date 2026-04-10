"""Tests for TUI-020: Notification badge for background events."""

from __future__ import annotations

from bernstein.tui.notification_badge import BadgeTracker


class TestBadgeTracker:
    def test_initial_zero(self) -> None:
        tracker = BadgeTracker()
        assert tracker.get_count("tasks") == 0
        assert tracker.get_count("logs") == 0

    def test_increment(self) -> None:
        tracker = BadgeTracker()
        tracker.increment("tasks")
        tracker.increment("tasks")
        assert tracker.get_count("tasks") == 2

    def test_clear_panel(self) -> None:
        tracker = BadgeTracker()
        tracker.increment("tasks")
        tracker.increment("tasks")
        tracker.clear("tasks")
        assert tracker.get_count("tasks") == 0

    def test_clear_does_not_affect_other(self) -> None:
        tracker = BadgeTracker()
        tracker.increment("tasks")
        tracker.increment("logs")
        tracker.clear("tasks")
        assert tracker.get_count("logs") == 1

    def test_clear_all(self) -> None:
        tracker = BadgeTracker()
        tracker.increment("tasks")
        tracker.increment("logs")
        tracker.clear_all()
        assert tracker.get_count("tasks") == 0
        assert tracker.get_count("logs") == 0

    def test_format_badge_zero(self) -> None:
        tracker = BadgeTracker()
        assert tracker.format_badge("tasks") == ""

    def test_format_badge_nonzero(self) -> None:
        tracker = BadgeTracker()
        tracker.increment("tasks")
        tracker.increment("tasks")
        tracker.increment("tasks")
        assert tracker.format_badge("tasks") == "[3 new]"

    def test_format_badge_alert(self) -> None:
        tracker = BadgeTracker()
        tracker.set_alert("logs")
        assert tracker.format_badge("logs") == "[!]"

    def test_alert_cleared_with_clear(self) -> None:
        tracker = BadgeTracker()
        tracker.set_alert("logs")
        tracker.clear("logs")
        assert tracker.format_badge("logs") == ""

    def test_has_unread(self) -> None:
        tracker = BadgeTracker()
        assert tracker.has_unread() is False
        tracker.increment("tasks")
        assert tracker.has_unread() is True

    def test_focused_panel_ignored(self) -> None:
        tracker = BadgeTracker()
        tracker.set_focused("tasks")
        tracker.increment("tasks")
        assert tracker.get_count("tasks") == 0
