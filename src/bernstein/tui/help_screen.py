"""Help screen modal for TUI — shortcuts plus discoverability hints."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class HelpScreen(ModalScreen[None]):
    """Modal screen showing shortcuts, palette tips, and recent actions."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }

    HelpScreen DataTable {
        width: 74;
        height: 18;
        background: $surface;
    }

    HelpScreen Static {
        width: 74;
        background: $surface;
        padding: 0 1;
    }

    #help-title {
        content-align: center middle;
        text-style: bold;
    }

    #help-hints, #help-recent {
        height: auto;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def __init__(
        self,
        *,
        recent_actions: list[str] | None = None,
        visible_panels: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._recent_actions = recent_actions or []
        self._visible_panels = visible_panels or []

    def compose(self) -> ComposeResult:
        """Compose the help screen."""
        yield Static("Keyboard Shortcuts", id="help-title")
        yield Static(id="help-hints")
        yield DataTable(id="help-table")
        yield Static(id="help-recent")

    def on_mount(self) -> None:
        """Populate the help table and discoverability hints on mount."""
        from bernstein.tui.keybinding_config import resolve_all_bindings

        hints = self.query_one("#help-hints", Static)
        table = cast("DataTable[str]", self.query_one("#help-table", DataTable))
        recent = self.query_one("#help-recent", Static)
        table.add_columns("Key", "Action")

        entries = resolve_all_bindings()
        for entry in entries:
            key_display = entry.key.replace("ctrl+", "Ctrl+").replace("escape", "Escape")
            table.add_row(key_display, entry.description)

        visible = ", ".join(self._visible_panels[:6]) if self._visible_panels else "task-list, task-context, agent-log"
        hints.update(
            "\n".join(
                [
                    "Use Ctrl+P to search commands and jump directly to tasks.",
                    "Use / to filter tasks by status, role, priority, or agent.",
                    f"Visible panels: {visible}",
                ]
            )
        )

        if self._recent_actions:
            recent.update("Recent palette actions: " + " • ".join(self._recent_actions[-5:]))
        else:
            recent.update("Recent palette actions: none yet")
