"""Tests for ApprovalAction message dispatch in the TUI approval panel.

``ApprovalAction`` is posted to the running app via ``post_message`` from
``ApprovalPanel.action_approve``/``action_reject``. Textual's message pump
requires anything passed to ``post_message`` to be a genuine ``Message``
instance (it checks for attributes only ``Message.__init__`` sets); a plain
dataclass fails that contract. These tests drive the real code path through
a minimal Textual app harness (App test pilot) so a regression here is
caught by an actual message-dispatch failure, not just a type-checker run.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.message import Message

from bernstein.tui.approval_panel import ApprovalAction, ApprovalEntry, ApprovalPanel


class _ApprovalHarnessApp(App[None]):
    """Minimal app that mounts ApprovalPanel and records dispatched actions."""

    def __init__(self) -> None:
        super().__init__()
        self.received: list[ApprovalAction] = []

    def compose(self) -> ComposeResult:
        yield ApprovalPanel()

    def on_approval_action(self, event: ApprovalAction) -> None:
        self.received.append(event)


def _one_entry(task_id: str = "t1") -> ApprovalEntry:
    return ApprovalEntry(
        task_id=task_id,
        task_title="Some task",
        session_id="s1",
        diff_preview="",
        test_summary="",
    )


class TestApprovalActionMessage:
    """ApprovalAction must be a real Message subclass, not a plain dataclass."""

    def test_approval_action_is_a_message_subclass(self) -> None:
        """post_message() only accepts Message instances - ApprovalAction must qualify."""
        assert issubclass(ApprovalAction, Message)

    @pytest.mark.asyncio
    async def test_action_approve_dispatches_to_handler(self) -> None:
        """action_approve() posts an ApprovalAction that a handler actually receives."""
        app = _ApprovalHarnessApp()
        async with app.run_test() as pilot:
            panel = app.query_one(ApprovalPanel)
            panel.refresh_entries([_one_entry("t1")])
            panel._selected_index = 0

            await panel.action_approve()
            await pilot.pause()

            assert len(app.received) == 1
            action = app.received[0]
            assert isinstance(action, ApprovalAction)
            assert action.approved is True
            assert action.task_id == "t1"
            assert action.reason == "Approved via TUI"

    @pytest.mark.asyncio
    async def test_action_reject_dispatches_to_handler(self) -> None:
        """action_reject() posts an ApprovalAction that a handler actually receives."""
        app = _ApprovalHarnessApp()
        async with app.run_test() as pilot:
            panel = app.query_one(ApprovalPanel)
            panel.refresh_entries([_one_entry("t2")])
            panel._selected_index = 0

            await panel.action_reject()
            await pilot.pause()

            assert len(app.received) == 1
            action = app.received[0]
            assert isinstance(action, ApprovalAction)
            assert action.approved is False
            assert action.task_id == "t2"
            assert action.reason == "Rejected via TUI"
