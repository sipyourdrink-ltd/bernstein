"""Task search and filter widget for TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Input

if TYPE_CHECKING:
    from textual.events import Key


class TaskSearchInput(Input):
    """Search input widget for filtering tasks.

    Press '/' to focus, Escape to clear.
    Filters task table in real-time as user types.
    """

    DEFAULT_CSS = """
    TaskSearchInput {
        dock: top;
        width: 100%;
        margin: 0 1 1 1;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, placeholder="Search tasks (by title, role, status, tags)...", **kwargs)

    def on_key(self, event: Key) -> None:
        """Handle key events.

        Args:
            event: Key event.
        """
        if event.key == "escape":
            # Clear search
            self.value = ""
            self.blur()
            event.stop()
