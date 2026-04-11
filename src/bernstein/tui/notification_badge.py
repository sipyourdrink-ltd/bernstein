"""TUI-020: Notification badge for background events.

Shows badge counts on panels indicating unread events when the user
is focused on another panel (e.g., "Tasks [3 new]", "Logs [!]").

UX-009: Extends with NotificationHistory for persistent unread tracking.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime

from rich.text import Text
from textual.widgets import Static


class BadgeTracker:
    """Tracks unread event counts per panel.

    When a panel is focused, events for that panel are ignored
    (the user is already looking at it).
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._alerts: set[str] = set()
        self._focused: str | None = None

    def set_focused(self, panel_id: str | None) -> None:
        """Set which panel is currently focused.

        Clears any badge for the newly focused panel.

        Args:
            panel_id: The panel ID that now has focus, or None.
        """
        self._focused = panel_id
        if panel_id:
            self.clear(panel_id)

    def increment(self, panel_id: str, count: int = 1) -> None:
        """Increment the unread count for a panel.

        Does nothing if that panel is currently focused.

        Args:
            panel_id: Target panel ID.
            count: Number to add.
        """
        if panel_id == self._focused:
            return
        self._counts[panel_id] += count

    def set_alert(self, panel_id: str) -> None:
        """Set an alert flag on a panel (shown as [!]).

        Args:
            panel_id: Target panel ID.
        """
        if panel_id == self._focused:
            return
        self._alerts.add(panel_id)

    def get_count(self, panel_id: str) -> int:
        """Get the unread count for a panel.

        Args:
            panel_id: Target panel ID.

        Returns:
            Current unread count.
        """
        return self._counts.get(panel_id, 0)

    def clear(self, panel_id: str) -> None:
        """Clear badge and alert for a panel.

        Args:
            panel_id: Target panel ID.
        """
        self._counts.pop(panel_id, None)
        self._alerts.discard(panel_id)

    def clear_all(self) -> None:
        """Clear all badges and alerts."""
        self._counts.clear()
        self._alerts.clear()

    def has_unread(self) -> bool:
        """Whether any panel has unread events.

        Returns:
            True if any panel has a count > 0 or an alert.
        """
        return bool(self._alerts) or any(v > 0 for v in self._counts.values())

    def format_badge(self, panel_id: str) -> str:
        """Format the badge text for a panel.

        Args:
            panel_id: Target panel ID.

        Returns:
            Badge string like "[3 new]", "[!]", or empty string.
        """
        if panel_id in self._alerts:
            return "[!]"
        count = self._counts.get(panel_id, 0)
        if count > 0:
            return f"[{count} new]"
        return ""


# ---------------------------------------------------------------------------
# UX-009: Notification history with unread tracking
# ---------------------------------------------------------------------------

#: Maximum number of entries retained in notification history.
_MAX_HISTORY: int = 100


@dataclass
class NotificationRecord:
    """A single entry in the notification history.

    Attributes:
        message: The notification text.
        level: Severity level (e.g. "info", "success", "warning", "error").
        timestamp: Unix timestamp when the notification was created.
        read: Whether the user has marked this notification as read.
        source: Optional source identifier (e.g. task_id, panel_id).
    """

    message: str
    level: str = "info"
    timestamp: float = field(default_factory=time.time)
    read: bool = False
    source: str = ""


class NotificationHistory:
    """Stores the last N notifications with read/unread state.

    Used by the notification center panel to display a scrollable
    history of all events, with unread badges.
    """

    def __init__(self, max_size: int = _MAX_HISTORY) -> None:
        """Initialise history with a bounded capacity.

        Args:
            max_size: Maximum number of records to retain.
        """
        self._max_size = max_size
        self._records: deque[NotificationRecord] = deque(maxlen=max_size)

    def add(
        self,
        message: str,
        level: str = "info",
        source: str = "",
        timestamp: float | None = None,
    ) -> NotificationRecord:
        """Add a new notification to history.

        Args:
            message: The notification text.
            level: Severity level string.
            source: Optional source identifier.
            timestamp: Unix timestamp (defaults to now).

        Returns:
            The created NotificationRecord.
        """
        record = NotificationRecord(
            message=message,
            level=level,
            timestamp=timestamp if timestamp is not None else time.time(),
            source=source,
        )
        self._records.append(record)
        return record

    def mark_read(self, index: int) -> None:
        """Mark a single notification as read by index (0 = oldest).

        Args:
            index: Zero-based index into the history list.

        Raises:
            IndexError: If the index is out of range.
        """
        if index < 0 or index >= len(self._records):
            raise IndexError(f"index {index} out of range (0..{len(self._records) - 1})")
        self._records[index].read = True

    def mark_all_read(self) -> None:
        """Mark every notification in the history as read."""
        for record in self._records:
            record.read = True

    def get_unread_count(self) -> int:
        """Return the number of unread notifications.

        Returns:
            Count of records where ``read`` is False.
        """
        return sum(1 for r in self._records if not r.read)

    def get_history(self, limit: int | None = None) -> list[NotificationRecord]:
        """Return notification history, newest first.

        Args:
            limit: Maximum number of records to return.
                None returns all records.

        Returns:
            List of NotificationRecord, newest first.
        """
        records = list(reversed(self._records))
        if limit is not None:
            return records[:limit]
        return records

    @property
    def size(self) -> int:
        """Total number of records in history."""
        return len(self._records)


def render_notification_center(
    records: list[NotificationRecord],
    *,
    unread_count: int = 0,
    limit: int = 5,
) -> Text:
    """Render notification history as compact Rich text.

    Args:
        records: Notification records in newest-first order.
        unread_count: Number of unread notifications.
        limit: Maximum number of entries to display.

    Returns:
        Rich ``Text`` payload suitable for a compact review panel.
    """
    text = Text.from_markup(
        "[bold]Notifications[/bold]"
        f" [dim]({unread_count} unread, N marks read)[/dim]"
    )
    entries = records[:limit]
    if not entries:
        text.append("\n")
        text.append("No notifications yet.", style="dim")
        return text

    level_styles: dict[str, str] = {
        "info": "cyan",
        "success": "green",
        "warning": "yellow",
        "error": "red",
    }
    for record in entries:
        label = record.level.upper()[:4].ljust(4)
        stamp = datetime.fromtimestamp(record.timestamp).strftime("%H:%M:%S")
        unread_marker = "new " if not record.read else "    "
        text.append("\n")
        text.append(stamp, style="dim")
        text.append(" ")
        text.append(unread_marker, style="bold" if not record.read else "dim")
        text.append(label, style=level_styles.get(record.level, "white"))
        text.append(" ")
        text.append(record.message)
    return text


class NotificationCenterPanel(Static):
    """Compact review panel for recent notifications and unread state."""

    DEFAULT_CSS = """
    NotificationCenterPanel {
        height: auto;
        max-height: 10;
        border-top: solid #333;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        """Initialise with an empty notification snapshot."""
        super().__init__(**kwargs)
        self._records: list[NotificationRecord] = []
        self._unread_count = 0

    def set_history(self, records: list[NotificationRecord], unread_count: int) -> None:
        """Replace the displayed notification snapshot and refresh.

        Args:
            records: Notification records in newest-first order.
            unread_count: Number of unread notifications.
        """
        self._records = records
        self._unread_count = unread_count
        self.refresh()

    def render(self) -> Text:
        """Render recent notification history."""
        return render_notification_center(self._records, unread_count=self._unread_count)
