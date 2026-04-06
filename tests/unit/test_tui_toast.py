"""Tests for TUI-009: Notification toast for events."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import time

from bernstein.tui.toast import (
    TOAST_COLORS,
    TOAST_ICONS,
    TOAST_LABELS,
    Toast,
    ToastLevel,
    ToastManager,
    render_toast,
    render_toast_stack,
)


class TestToast:
    def test_defaults(self) -> None:
        toast = Toast(message="Hello")
        assert toast.level == ToastLevel.INFO
        assert toast.duration_s == 5.0
        assert toast.source == ""

    def test_not_expired_immediately(self) -> None:
        toast = Toast(message="Hello", duration_s=5.0)
        assert toast.is_expired() is False

    def test_expired_after_duration(self) -> None:
        old_ts = time.time() - 10
        toast = Toast(message="Hello", duration_s=5.0, timestamp=old_ts)
        assert toast.is_expired() is True

    def test_remaining_seconds(self) -> None:
        toast = Toast(message="Hello", duration_s=5.0)
        assert toast.remaining_s > 0
        assert toast.remaining_s <= 5.0


class TestToastLevel:
    def test_all_levels_have_colors(self) -> None:
        for level in ToastLevel:
            assert level in TOAST_COLORS

    def test_all_levels_have_icons(self) -> None:
        for level in ToastLevel:
            assert level in TOAST_ICONS

    def test_all_levels_have_labels(self) -> None:
        for level in ToastLevel:
            assert level in TOAST_LABELS


class TestToastManager:
    def test_add_toast(self) -> None:
        mgr = ToastManager()
        toast = mgr.add("Test message")
        assert toast.message == "Test message"
        assert mgr.count == 1

    def test_task_completed(self) -> None:
        mgr = ToastManager()
        toast = mgr.task_completed("task-123", "Fix bug")
        assert toast.level == ToastLevel.SUCCESS
        assert "task-123" in toast.message

    def test_agent_killed(self) -> None:
        mgr = ToastManager()
        toast = mgr.agent_killed("sess-abc", "backend")
        assert toast.level == ToastLevel.WARNING
        assert "sess-abc" in toast.message

    def test_budget_warning(self) -> None:
        mgr = ToastManager()
        toast = mgr.budget_warning(9.0, 10.0)
        assert toast.level == ToastLevel.WARNING
        assert "90%" in toast.message

    def test_error(self) -> None:
        mgr = ToastManager()
        toast = mgr.error("Something broke")
        assert toast.level == ToastLevel.ERROR

    def test_prune_expired(self) -> None:
        mgr = ToastManager()
        old_ts = time.time() - 100
        mgr._active.append(Toast(message="Old", timestamp=old_ts, duration_s=5.0))
        mgr.add("New")
        pruned = mgr.prune()
        assert pruned == 1
        assert mgr.count == 1

    def test_dismiss_all(self) -> None:
        mgr = ToastManager()
        mgr.add("One")
        mgr.add("Two")
        mgr.dismiss_all()
        assert mgr.count == 0

    def test_active_toasts_filters_expired(self) -> None:
        mgr = ToastManager()
        old_ts = time.time() - 100
        mgr._active.append(Toast(message="Expired", timestamp=old_ts, duration_s=5.0))
        mgr.add("Active")
        active = mgr.active_toasts
        assert len(active) == 1
        assert active[0].message == "Active"

    def test_history_preserved(self) -> None:
        mgr = ToastManager()
        mgr.add("First")
        mgr.add("Second")
        mgr.dismiss_all()
        assert len(mgr.history) == 2

    def test_max_visible_capped(self) -> None:
        mgr = ToastManager()
        for i in range(10):
            mgr.add(f"Toast {i}")
        # Active deque is capped at MAX_VISIBLE
        assert len(mgr._active) <= ToastManager.MAX_VISIBLE


class TestRenderToast:
    def test_render_info(self) -> None:
        toast = Toast(message="Info message", level=ToastLevel.INFO)
        text = render_toast(toast)
        assert "Info message" in text.plain

    def test_render_accessible(self) -> None:
        toast = Toast(message="Error occurred", level=ToastLevel.ERROR)
        text = render_toast(toast, accessible=True)
        assert "ERR" in text.plain

    def test_render_truncated(self) -> None:
        toast = Toast(message="x" * 200)
        text = render_toast(toast, width=30)
        assert "..." in text.plain


class TestRenderToastStack:
    def test_empty_stack(self) -> None:
        mgr = ToastManager()
        text = render_toast_stack(mgr)
        assert text.plain == ""

    def test_multiple_toasts(self) -> None:
        mgr = ToastManager()
        mgr.add("First")
        mgr.add("Second")
        text = render_toast_stack(mgr)
        assert "First" in text.plain
        assert "Second" in text.plain
