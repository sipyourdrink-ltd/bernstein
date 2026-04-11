"""Command palette with fuzzy search for TUI actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


@dataclass(frozen=True)
class PaletteCommand:
    """A single command registered in the palette."""

    name: str
    action: str
    description: str = ""
    keybinding: str = ""
    category: str = "general"


def fuzzy_match(query: str, text: str) -> tuple[bool, int]:
    """Fuzzy match a query against text."""
    q = query.lower()
    t = text.lower()
    if not q:
        return True, 0
    if q in t:
        return True, 0
    if t.startswith(q):
        return True, 1

    qi = 0
    score = 0
    last_match_pos = -1
    for index, char in enumerate(t):
        if qi < len(q) and char == q[qi]:
            if last_match_pos >= 0:
                score += index - last_match_pos - 1
            last_match_pos = index
            qi += 1

    if qi == len(q):
        return True, score + 10
    return False, 999


@dataclass
class CommandPalette:
    """Palette registry and fuzzy-search state."""

    commands: list[PaletteCommand] = field(default_factory=list[PaletteCommand])
    query: str = ""
    selected_index: int = 0

    def register(self, command: PaletteCommand) -> None:
        self.commands.append(command)

    def register_many(self, commands: list[PaletteCommand]) -> None:
        self.commands.extend(commands)

    def search(self, query: str | None = None) -> list[PaletteCommand]:
        if query is not None:
            self.query = query
        q = self.query.strip()
        if not q:
            return list(self.commands)

        scored: list[tuple[int, PaletteCommand]] = []
        for command in self.commands:
            best_match = False
            best_score = 999
            for text in (command.name, command.action, command.description, command.category):
                match, score = fuzzy_match(q, text)
                if match and score < best_score:
                    best_match = True
                    best_score = score
            if best_match:
                scored.append((best_score, command))

        scored.sort(key=lambda item: item[0])
        return [command for _, command in scored]

    def set_query(self, query: str) -> None:
        self.query = query
        self.selected_index = 0

    def move_selection(self, delta: int) -> None:
        results = self.search()
        if not results:
            self.selected_index = 0
            return
        self.selected_index = max(0, min(len(results) - 1, self.selected_index + delta))

    def get_selected(self) -> PaletteCommand | None:
        results = self.search()
        if not results or self.selected_index >= len(results):
            return None
        return results[self.selected_index]

    def clear(self) -> None:
        self.query = ""
        self.selected_index = 0


DEFAULT_PALETTE_COMMANDS: list[PaletteCommand] = [
    PaletteCommand("Quit", "quit", "Exit the TUI", "q", "navigation"),
    PaletteCommand("Refresh", "refresh", "Refresh task data", "r", "navigation"),
    PaletteCommand("Hard Stop", "hard_stop", "Stop all agents immediately", "S", "control"),
    PaletteCommand("Spawn Agent", "spawn_now", "Spawn a new agent", "s", "control"),
    PaletteCommand("Kill Agent", "kill_agent", "Kill the selected agent", "k", "control"),
    PaletteCommand("Cancel Task", "cancel_task", "Cancel the selected task", "x", "control"),
    PaletteCommand("Retry Task", "retry_task", "Retry a failed task", "t", "control"),
    PaletteCommand("Prioritize", "prioritize", "Prioritize selected task", "p", "control"),
    PaletteCommand("Toggle Timeline", "toggle_timeline", "Show/hide timeline view", "v", "view"),
    PaletteCommand("Toggle Waterfall", "toggle_waterfall", "Show/hide waterfall view", "f", "view"),
    PaletteCommand("Toggle Scratchpad", "toggle_scratchpad", "Show/hide scratchpad", "c", "view"),
    PaletteCommand("Toggle Coordinator", "toggle_coordinator", "Show/hide coordinator view", "w", "view"),
    PaletteCommand("Toggle Approvals", "toggle_approvals", "Show/hide approval panel", "a", "view"),
    PaletteCommand("Toggle Tool Observer", "toggle_tool_observer", "Show/hide tool calls", "l", "view"),
    PaletteCommand("Toggle Split Pane", "toggle_split_pane", "Toggle split-pane layout", "Ctrl+L", "view"),
    PaletteCommand("Copy Task ID", "copy_to_clipboard", "Copy task ID to clipboard", "Ctrl+Y", "clipboard"),
    PaletteCommand("Cycle Theme", "cycle_theme", "Switch dark/light/high-contrast", "Ctrl+T", "appearance"),
    PaletteCommand("Toggle Accessibility", "toggle_accessibility", "Toggle accessibility mode", "Ctrl+A", "appearance"),
    PaletteCommand("Show Help", "show_help", "Show keyboard shortcuts", "?", "help"),
]


def render_palette_item(
    command: PaletteCommand,
    *,
    selected: bool = False,
    query: str = "",
) -> Text:
    """Render a single palette item as Rich Text."""
    text = Text()
    style = "reverse" if selected else ""
    text.append(f" {command.category[:4]:4s} ", style=f"dim {style}")
    text.append(" ")

    name = command.name
    if query:
        query_lower = query.lower()
        name_lower = name.lower()
        pos = name_lower.find(query_lower)
        if pos >= 0:
            text.append(name[:pos], style=f"bold {style}")
            text.append(name[pos : pos + len(query)], style=f"bold underline {style}")
            text.append(name[pos + len(query) :], style=f"bold {style}")
        else:
            text.append(name, style=f"bold {style}")
    else:
        text.append(name, style=f"bold {style}")

    if command.description:
        text.append(f"  {command.description}", style=f"dim {style}")
    if command.keybinding:
        text.append(f"  [{command.keybinding}]", style=f"cyan {style}")
    return text


def render_palette(
    palette: CommandPalette,
    *,
    max_visible: int = 10,
) -> Text:
    """Render the full command palette as Rich Text."""
    text = Text()
    text.append("> ", style="cyan bold")
    text.append(palette.query or "", style="bold")
    text.append("\n")
    text.append("\u2500" * 40, style="dim")
    text.append("\n")

    results = palette.search()
    visible = results[:max_visible]
    for index, command in enumerate(visible):
        text.append_text(render_palette_item(command, selected=index == palette.selected_index, query=palette.query))
        text.append("\n")

    if len(results) > max_visible:
        text.append(f"  ... and {len(results) - max_visible} more\n", style="dim")
    if not results:
        text.append("  No matching commands\n", style="dim")
    return text


class CommandPaletteScreen(ModalScreen[str | None]):
    """Modal command palette with fuzzy search and keyboard execution."""

    DEFAULT_CSS = """
    CommandPaletteScreen {
        align: center top;
        padding-top: 2;
    }

    #command-palette-shell {
        width: 88;
        max-width: 92%;
        height: auto;
        border: round $primary 40%;
        background: $surface;
        padding: 1;
    }

    #command-palette-input {
        width: 100%;
        margin-bottom: 1;
    }

    #command-palette-results {
        min-height: 8;
        max-height: 18;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("escape", "cancel", "Close"),
        ("down", "move_down", "Next"),
        ("up", "move_up", "Previous"),
        ("enter", "execute_selected", "Run"),
    ]

    def __init__(self, palette: CommandPalette | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._palette = palette or CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))

    def compose(self) -> ComposeResult:
        with Vertical(id="command-palette-shell"):
            yield Input(placeholder="Search commands or jump to a task...", id="command-palette-input")
            yield Static(id="command-palette-results")

    def on_mount(self) -> None:
        self.query_one("#command-palette-input", Input).focus()
        self._refresh_results()

    def _refresh_results(self) -> None:
        self.query_one("#command-palette-results", Static).update(render_palette(self._palette, max_visible=12))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "command-palette-input":
            return
        self._palette.set_query(event.value)
        self._refresh_results()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_move_down(self) -> None:
        self._palette.move_selection(1)
        self._refresh_results()

    def action_move_up(self) -> None:
        self._palette.move_selection(-1)
        self._refresh_results()

    def action_execute_selected(self) -> None:
        selected = self._palette.get_selected()
        self.dismiss(selected.action if selected is not None else None)
