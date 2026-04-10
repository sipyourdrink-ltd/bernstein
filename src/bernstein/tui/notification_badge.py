"""TUI-020: Notification badge for background events.

Shows badge counts on panels indicating unread events when the user
is focused on another panel (e.g., "Tasks [3 new]", "Logs [!]").
"""

from __future__ import annotations

from collections import defaultdict


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
