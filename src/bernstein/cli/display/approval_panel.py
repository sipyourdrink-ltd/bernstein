"""Textual widget that surfaces pending tool-call approvals.

The panel is mounted into the dashboard via the ``add_section`` helper
on :class:`bernstein.cli.dashboard_app.BernsteinApp`, so the existing
layout is preserved and only one container is touched.

Three buttons per pending entry drive the decision:

* **Approve** — allow this single invocation.
* **Reject** — deny the call.
* **Always** — allow and promote the pattern into the operator's
  ``always_allow`` rules.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static

from bernstein.core.approval.models import ApprovalDecision, PendingApproval
from bernstein.core.approval.queue import ApprovalQueue, get_default_queue

if TYPE_CHECKING:
    from textual.app import ComposeResult


class ApprovalResolved(Message):
    """Emitted when an operator resolves an approval from the panel.

    Attributes:
        approval_id: Id of the resolved approval.
        decision: The button the operator clicked.
    """

    def __init__(self, approval_id: str, decision: ApprovalDecision) -> None:
        self.approval_id = approval_id
        self.decision = decision
        super().__init__()


class ApprovalRow(Static):
    """A single pending approval row with Approve/Reject/Always buttons."""

    def __init__(self, approval: PendingApproval, *, queue: ApprovalQueue) -> None:
        super().__init__()
        self._approval = approval
        self._queue = queue

    def compose(self) -> ComposeResult:
        """Lay out the approval summary and the three action buttons."""
        summary = Text()
        summary.append(f" {self._approval.tool_name}", style="bold bright_yellow")
        summary.append(f"  {self._approval.agent_role}", style="dim")
        args_preview = ", ".join(f"{k}={v!r}" for k, v in self._approval.tool_args.items())
        if len(args_preview) > 80:
            args_preview = args_preview[:77] + "..."
        summary.append(f"\n  {args_preview}", style="dim")
        yield Static(summary, classes="approval-summary")
        with Horizontal(classes="approval-buttons"):
            yield Button("Approve", id=f"approve-{self._approval.id}", variant="success")
            yield Button("Reject", id=f"reject-{self._approval.id}", variant="error")
            yield Button("Always", id=f"always-{self._approval.id}", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Translate a button click into a queue resolution + panel message."""
        bid = event.button.id or ""
        if bid.startswith("approve-"):
            decision = ApprovalDecision.ALLOW
        elif bid.startswith("reject-"):
            decision = ApprovalDecision.REJECT
        elif bid.startswith("always-"):
            decision = ApprovalDecision.ALWAYS
        else:
            return
        # Another resolver (web UI, CLI) may win the race; swallow the
        # KeyError so the panel just refreshes on its next tick.
        with contextlib.suppress(KeyError):
            self._queue.resolve(self._approval.id, decision, reason="tui")
        self.post_message(ApprovalResolved(self._approval.id, decision))


class ApprovalPanel(Vertical):
    """Dashboard panel that lists pending approvals and refreshes on a timer."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "approve_focused", "Approve", show=False),
        Binding("r", "reject_focused", "Reject", show=False),
    ]

    DEFAULT_CSS = """
    ApprovalPanel {
        height: auto;
        border: solid $warning;
        padding: 0 1;
        margin: 1 0;
    }
    ApprovalPanel .approval-header {
        color: $warning;
        text-style: bold;
    }
    ApprovalPanel .approval-summary {
        padding: 0 0 0 1;
    }
    ApprovalPanel .approval-buttons {
        height: 3;
        align: left middle;
    }
    ApprovalPanel Button {
        margin: 0 1 0 0;
    }
    """

    def __init__(
        self,
        *,
        queue: ApprovalQueue | None = None,
        session_id: str | None = None,
        refresh_seconds: float = 1.0,
    ) -> None:
        super().__init__(id="approval-panel")
        self._queue = queue if queue is not None else get_default_queue()
        self._session_id = session_id
        self._refresh_seconds = refresh_seconds
        self._seen_ids: set[str] = set()

    def compose(self) -> ComposeResult:
        """Render the static header; rows are mounted dynamically."""
        yield Static(" APPROVALS", classes="approval-header")
        yield Static("[dim]No pending approvals[/dim]", id="approval-empty")

    def on_mount(self) -> None:
        """Kick off the refresh timer when the widget is attached."""
        self.set_interval(self._refresh_seconds, self.refresh_pending)
        self.refresh_pending()

    def refresh_pending(self) -> None:
        """Reconcile on-screen rows with the current queue state."""
        pending = self._queue.list_pending(session_id=self._session_id)
        current_ids = {approval.id for approval in pending}

        # Drop rows whose approvals are no longer pending.
        # Note: self.query(...) returns an already-iterable DOMQuery, so we
        # iterate it directly. Removing rows mid-iteration is safe here
        # because DOMQuery snapshots its match set (Sonar python:S7504).
        for row in self.query(ApprovalRow):
            if row._approval.id not in current_ids:
                row.remove()
                self._seen_ids.discard(row._approval.id)

        empty_hint = next(iter(self.query("#approval-empty")), None)
        if pending:
            if empty_hint is not None:
                empty_hint.remove()
            for approval in pending:
                if approval.id in self._seen_ids:
                    continue
                self._seen_ids.add(approval.id)
                self.mount(ApprovalRow(approval, queue=self._queue))
        elif empty_hint is None:
            self.mount(Static("[dim]No pending approvals[/dim]", id="approval-empty"))


__all__ = ["ApprovalPanel", "ApprovalResolved", "ApprovalRow"]
