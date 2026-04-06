"""TUI-012: Command palette with fuzzy search for TUI actions.

Provides a searchable command palette (opened with Ctrl+P) that lets
users fuzzy-search and execute any available TUI action. Similar to
VS Code's command palette.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text


@dataclass(frozen=True)
class PaletteCommand:
    """A single command registered in the palette.

    Attributes:
        name: Display name (e.g. "Toggle Split Pane").
        action: Action identifier (e.g. "toggle_split_pane").
        description: Brief description of what the command does.
        keybinding: Optional display string for the keybinding.
        category: Command category for grouping.
    """

    name: str
    action: str
    description: str = ""
    keybinding: str = ""
    category: str = "general"


def fuzzy_match(query: str, text: str) -> tuple[bool, int]:
    """Fuzzy match a query against text.

    Returns whether the query matches and a score (lower is better).
    Characters must appear in order but can be non-contiguous.

    Args:
        query: Search query (case-insensitive).
        text: Text to match against (case-insensitive).

    Returns:
        Tuple of (matches, score). Score is 0 for exact matches,
        higher for fuzzier matches.
    """
    q = query.lower()
    t = text.lower()

    if not q:
        return True, 0

    # Exact substring match is highest priority
    if q in t:
        return True, 0

    # Prefix match
    if t.startswith(q):
        return True, 1

    # Fuzzy: all chars must appear in order
    qi = 0
    score = 0
    last_match_pos = -1
    for ti, char in enumerate(t):
        if qi < len(q) and char == q[qi]:
            # Penalize gaps
            if last_match_pos >= 0:
                gap = ti - last_match_pos - 1
                score += gap
            last_match_pos = ti
            qi += 1

    if qi == len(q):
        return True, score + 10  # Base penalty for fuzzy vs exact
    return False, 999


@dataclass
class CommandPalette:
    """The command palette state and search logic.

    Maintains a registry of available commands and provides fuzzy
    search functionality.
    """

    commands: list[PaletteCommand] = field(default_factory=list[PaletteCommand])
    query: str = ""
    selected_index: int = 0

    def register(self, command: PaletteCommand) -> None:
        """Register a command in the palette.

        Args:
            command: PaletteCommand to register.
        """
        self.commands.append(command)

    def register_many(self, commands: list[PaletteCommand]) -> None:
        """Register multiple commands at once.

        Args:
            commands: List of PaletteCommand to register.
        """
        self.commands.extend(commands)

    def search(self, query: str | None = None) -> list[PaletteCommand]:
        """Search commands by fuzzy matching.

        Args:
            query: Search query. If None, uses the current query state.

        Returns:
            List of matching commands sorted by relevance.
        """
        if query is not None:
            self.query = query
        q = self.query.strip()

        if not q:
            return list(self.commands)

        scored: list[tuple[int, PaletteCommand]] = []
        for cmd in self.commands:
            # Match against name, action, description, and category
            best_match = False
            best_score = 999
            for text in (cmd.name, cmd.action, cmd.description, cmd.category):
                match, score = fuzzy_match(q, text)
                if match and score < best_score:
                    best_match = True
                    best_score = score
            if best_match:
                scored.append((best_score, cmd))

        scored.sort(key=lambda x: x[0])
        return [cmd for _, cmd in scored]

    def set_query(self, query: str) -> None:
        """Update the search query and reset selection.

        Args:
            query: New search query.
        """
        self.query = query
        self.selected_index = 0

    def move_selection(self, delta: int) -> None:
        """Move the selection cursor by delta.

        Args:
            delta: Number of positions to move (positive=down, negative=up).
        """
        results = self.search()
        if not results:
            self.selected_index = 0
            return
        self.selected_index = max(0, min(len(results) - 1, self.selected_index + delta))

    def get_selected(self) -> PaletteCommand | None:
        """Get the currently selected command.

        Returns:
            Selected PaletteCommand, or None if nothing matches.
        """
        results = self.search()
        if not results or self.selected_index >= len(results):
            return None
        return results[self.selected_index]

    def clear(self) -> None:
        """Clear the search query and reset selection."""
        self.query = ""
        self.selected_index = 0


# Default commands for the Bernstein TUI
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
    cmd: PaletteCommand,
    *,
    selected: bool = False,
    query: str = "",
) -> Text:
    """Render a single palette item as Rich Text.

    Args:
        cmd: The command to render.
        selected: Whether this item is currently selected.
        query: Current search query for highlighting matches.

    Returns:
        Rich Text with the rendered item.
    """
    text = Text()
    style = "reverse" if selected else ""

    # Category badge
    text.append(f" {cmd.category[:4]:4s} ", style=f"dim {style}")
    text.append(" ")

    # Command name with match highlighting
    name = cmd.name
    if query:
        q_lower = query.lower()
        n_lower = name.lower()
        pos = n_lower.find(q_lower)
        if pos >= 0:
            text.append(name[:pos], style=f"bold {style}")
            text.append(name[pos : pos + len(query)], style=f"bold underline {style}")
            text.append(name[pos + len(query) :], style=f"bold {style}")
        else:
            text.append(name, style=f"bold {style}")
    else:
        text.append(name, style=f"bold {style}")

    # Description
    if cmd.description:
        text.append(f"  {cmd.description}", style=f"dim {style}")

    # Keybinding
    if cmd.keybinding:
        text.append(f"  [{cmd.keybinding}]", style=f"cyan {style}")

    return text


def render_palette(
    palette: CommandPalette,
    *,
    max_visible: int = 10,
) -> Text:
    """Render the full command palette as Rich Text.

    Args:
        palette: CommandPalette state.
        max_visible: Maximum number of results to show.

    Returns:
        Rich Text with the complete palette display.
    """
    text = Text()
    # Header
    text.append("> ", style="cyan bold")
    text.append(palette.query or "", style="bold")
    text.append("\n")
    text.append("\u2500" * 40, style="dim")
    text.append("\n")

    results = palette.search()
    visible = results[:max_visible]

    for i, cmd in enumerate(visible):
        item = render_palette_item(
            cmd,
            selected=(i == palette.selected_index),
            query=palette.query,
        )
        text.append_text(item)
        text.append("\n")

    if len(results) > max_visible:
        remaining = len(results) - max_visible
        text.append(f"  ... and {remaining} more", style="dim")
        text.append("\n")

    if not results:
        text.append("  No matching commands", style="dim")
        text.append("\n")

    return text
