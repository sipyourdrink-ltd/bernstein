"""Notification delivery tests for :class:`~bernstein.tui.app.BernsteinApp`.

Two notification surfaces share the app and must not interfere:

* ``self.notify(...)`` -- Textual's own transient toast, used by
  :mod:`bernstein.tui.approval_panel` to confirm a sent approval.
* The notification centre panel -- our persistent, read/unread history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.widgets._toast import Toast as TextualToast

from bernstein.tui.app import BernsteinApp
from bernstein.tui.approval_panel import ApprovalAction, ApprovalPanel
from bernstein.tui.notification_badge import NotificationCenterPanel
from bernstein.tui.toast import ToastLevel

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mount the app in an empty directory so it reads no repository state."""
    monkeypatch.chdir(tmp_path)


def _notification_center_text(app: BernsteinApp) -> str:
    """Return the plain text the notification centre panel currently renders."""
    return app.query_one("#notification-center", NotificationCenterPanel).render().plain


class TestTextualToasts:
    """``App.notify`` must reach the screen, not die inside the message pump.

    ``run_test`` suppresses the toast rack unless ``notifications=True``, so
    every test here opts in -- otherwise the assertions would pass against a
    subsystem that was never switched on.
    """

    @pytest.mark.asyncio
    async def test_notify_renders_a_toast(self) -> None:
        app = BernsteinApp()
        async with app.run_test(notifications=True) as pilot:
            app.notify("Approval sent: task-42")
            await pilot.pause()

            toasts = list(app.query(TextualToast))
            assert len(toasts) == 1
            assert "Approval sent: task-42" in toasts[0].render().plain

    @pytest.mark.asyncio
    async def test_notify_renders_a_toast_per_notification(self) -> None:
        app = BernsteinApp()
        async with app.run_test(notifications=True) as pilot:
            app.notify("first")
            app.notify("second", severity="error")
            await pilot.pause()

            rendered = {toast.render().plain for toast in app.query(TextualToast)}
            assert rendered == {"first", "second"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("approved", "expected"), [(True, "approved"), (False, "rejected")])
    async def test_approval_confirmation_reaches_the_operator(self, approved: bool, expected: str) -> None:
        """The confirmation an operator gets after acting on a tool call."""
        app = BernsteinApp()
        async with app.run_test(notifications=True) as pilot:
            panel = app.query_one(ApprovalPanel)
            panel.post_message(ApprovalAction(approved=approved, task_id="task-42"))
            await pilot.pause()

            toasts = list(app.query(TextualToast))
            assert len(toasts) == 1
            assert toasts[0].render().plain == f"Approval sent: {expected}"


class TestNotificationCentre:
    """Our own history keeps driving the notification centre panel."""

    @pytest.mark.asyncio
    async def test_starts_empty(self) -> None:
        app = BernsteinApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "No notifications yet." in _notification_center_text(app)

    @pytest.mark.asyncio
    async def test_remembered_toasts_render_as_unread_history(self) -> None:
        app = BernsteinApp()
        async with app.run_test() as pilot:
            app._remember_toast(app._toasts.add("theme switched", level=ToastLevel.INFO))
            app._remember_toast(app._toasts.error("worktree missing"))
            await pilot.pause()

            text = _notification_center_text(app)
            assert "(2 unread" in text
            assert "theme switched" in text
            assert "worktree missing" in text
            # Newest first, each flagged unread.
            assert text.index("worktree missing") < text.index("theme switched")
            assert text.count("new ") == 2

    @pytest.mark.asyncio
    async def test_acknowledge_clears_the_unread_marker(self) -> None:
        app = BernsteinApp()
        async with app.run_test() as pilot:
            app._remember_toast(app._toasts.error("worktree missing"))
            await pilot.pause()

            app.action_acknowledge_notifications()
            await pilot.pause()

            text = _notification_center_text(app)
            assert "(0 unread" in text
            assert "worktree missing" in text
            assert "new " not in text

    @pytest.mark.asyncio
    async def test_history_panel_shows_only_the_five_newest(self) -> None:
        app = BernsteinApp()
        async with app.run_test() as pilot:
            for index in range(7):
                app._remember_toast(app._toasts.add(f"event-{index}", level=ToastLevel.INFO))
            await pilot.pause()

            text = _notification_center_text(app)
            assert "(7 unread" in text
            assert "event-0" not in text
            assert "event-1" not in text
            for index in range(2, 7):
                assert f"event-{index}" in text
