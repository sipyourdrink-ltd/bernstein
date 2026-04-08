"""Help screen modal for TUI — shows all keyboard shortcuts."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class HelpScreen(ModalScreen[None]):
    """Modal screen showing all keyboard shortcuts.

    Displays a two-column table: Key | Action
    Activated by pressing '?' in the TUI.
    """

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }

    HelpScreen DataTable {
        width: 60;
        height: 20;
        background: $surface;
    }

    HelpScreen Static {
        width: 60;
        content-align: center middle;
        background: $surface;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        """Compose the help screen."""
        yield Static("Keyboard Shortcuts", id="help-title")
        yield DataTable(id="help-table")

    def on_mount(self) -> None:
        """Populate the help table on mount."""
        from bernstein.tui.keybinding_config import resolve_all_bindings

        table = cast("DataTable[str]", self.query_one("#help-table", DataTable))
        table.add_columns("Key", "Action")

        entries = resolve_all_bindings()
        for entry in entries:
            key_display = entry.key.replace("ctrl+", "Ctrl+").replace("escape", "Escape")
            table.add_row(key_display, entry.description)
