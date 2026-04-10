"""TUI-014: Vim-mode keybindings for TUI navigation.

Provides a standalone state machine for vim-style keybindings in the
Bernstein TUI. Handles three modes -- normal, command, and search --
with standard vim navigation keys (hjkl, gg, G, :, /).

This module is intentionally decoupled from ``app.py``. The TUI app
feeds key events into :class:`VimState` and reads back the resulting
:class:`VimAction` to decide what to do.

Example::

    state = VimState()
    action = state.handle_key("j")
    if action.kind == VimActionKind.SCROLL_DOWN:
        scroll_viewport(1)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class VimMode(Enum):
    """Active input mode for the vim state machine.

    Attributes:
        NORMAL: Default mode -- navigation keys are active.
        COMMAND: After pressing ``:``, collecting a command string.
        SEARCH: After pressing ``/``, collecting a search pattern.
    """

    NORMAL = auto()
    COMMAND = auto()
    SEARCH = auto()


class VimActionKind(Enum):
    """Semantic action produced by a key press.

    Each variant maps to a single TUI operation. The TUI app
    switches on this enum to execute the corresponding behaviour.
    """

    NONE = auto()
    SCROLL_UP = auto()
    SCROLL_DOWN = auto()
    SCROLL_LEFT = auto()
    SCROLL_RIGHT = auto()
    GOTO_TOP = auto()
    GOTO_BOTTOM = auto()
    HALF_PAGE_UP = auto()
    HALF_PAGE_DOWN = auto()
    ENTER_COMMAND = auto()
    ENTER_SEARCH = auto()
    SUBMIT_COMMAND = auto()
    SUBMIT_SEARCH = auto()
    CANCEL = auto()
    APPEND_CHAR = auto()
    BACKSPACE = auto()


@dataclass(frozen=True)
class VimAction:
    """Result of processing a single key event.

    Attributes:
        kind: Semantic action kind.
        payload: Extra data -- the full buffer for SUBMIT_* actions,
            or the appended character for APPEND_CHAR.
    """

    kind: VimActionKind
    payload: str = ""


@dataclass
class VimState:
    """Vim-mode state machine.

    Tracks the current mode and any pending key sequences (e.g. the
    ``g`` prefix for ``gg``). Feed keys via :meth:`handle_key` and
    inspect the returned :class:`VimAction`.

    Attributes:
        mode: Current input mode.
        buffer: Character buffer for command/search input.
        pending: Pending prefix key (e.g. ``"g"`` waiting for the
            second ``g`` in the ``gg`` sequence).
        enabled: When False, :meth:`handle_key` returns NONE for
            every key. Lets the TUI toggle vim mode on/off.
    """

    mode: VimMode = VimMode.NORMAL
    buffer: str = ""
    pending: str = ""
    enabled: bool = True
    _count_prefix: str = field(default="", repr=False)

    # ---- public API ----

    def handle_key(self, key: str) -> VimAction:
        """Process a single key event and return the resulting action.

        Args:
            key: Key name as delivered by the TUI framework. Single
                characters are passed as-is (``"j"``, ``"G"``).
                Special keys use their Textual names (``"escape"``,
                ``"enter"``, ``"backspace"``).

        Returns:
            A VimAction describing what the TUI should do.
        """
        if not self.enabled:
            return _NONE

        if self.mode is VimMode.NORMAL:
            return self._handle_normal(key)
        if self.mode is VimMode.COMMAND:
            return self._handle_input(key, VimActionKind.SUBMIT_COMMAND)
        # VimMode.SEARCH
        return self._handle_input(key, VimActionKind.SUBMIT_SEARCH)

    def reset(self) -> None:
        """Reset to initial normal mode, clearing all state."""
        self.mode = VimMode.NORMAL
        self.buffer = ""
        self.pending = ""
        self._count_prefix = ""

    # ---- private helpers ----

    def _handle_normal(self, key: str) -> VimAction:
        """Handle a key press in normal mode."""
        # If there is a pending 'g', check for 'gg'
        if self.pending == "g":
            self.pending = ""
            if key == "g":
                return VimAction(VimActionKind.GOTO_TOP)
            # Not gg -- fall through to handle key independently

        # Numeric count prefix accumulation
        if key.isdigit() and (self._count_prefix or key != "0"):
            self._count_prefix += key
            return _NONE

        count = int(self._count_prefix) if self._count_prefix else 1
        self._count_prefix = ""

        return self._dispatch_normal(key, count)

    def _dispatch_normal(self, key: str, count: int) -> VimAction:
        """Dispatch a normal-mode key with an optional repeat count.

        Args:
            key: The key to dispatch.
            count: Repeat count from numeric prefix (default 1).

        Returns:
            The resulting VimAction.
        """
        if key == "j" or key == "down":
            return VimAction(VimActionKind.SCROLL_DOWN, str(count))
        if key == "k" or key == "up":
            return VimAction(VimActionKind.SCROLL_UP, str(count))
        if key == "h" or key == "left":
            return VimAction(VimActionKind.SCROLL_LEFT, str(count))
        if key == "l" or key == "right":
            return VimAction(VimActionKind.SCROLL_RIGHT, str(count))
        if key == "G":
            return VimAction(VimActionKind.GOTO_BOTTOM)
        if key == "g":
            self.pending = "g"
            return _NONE
        if key == "ctrl+u":
            return VimAction(VimActionKind.HALF_PAGE_UP, str(count))
        if key == "ctrl+d":
            return VimAction(VimActionKind.HALF_PAGE_DOWN, str(count))
        if key == ":":
            self.mode = VimMode.COMMAND
            self.buffer = ""
            return VimAction(VimActionKind.ENTER_COMMAND)
        if key == "/":
            self.mode = VimMode.SEARCH
            self.buffer = ""
            return VimAction(VimActionKind.ENTER_SEARCH)

        return _NONE

    def _handle_input(self, key: str, submit_kind: VimActionKind) -> VimAction:
        """Handle a key press in command or search mode.

        Args:
            key: The key to process.
            submit_kind: The action kind to emit on Enter.

        Returns:
            The resulting VimAction.
        """
        if key == "escape":
            payload = self.buffer
            self.reset()
            return VimAction(VimActionKind.CANCEL, payload)

        if key == "enter":
            payload = self.buffer
            self.reset()
            return VimAction(submit_kind, payload)

        if key == "backspace":
            if self.buffer:
                self.buffer = self.buffer[:-1]
            return VimAction(VimActionKind.BACKSPACE, self.buffer)

        # Single printable character
        if len(key) == 1 and key.isprintable():
            self.buffer += key
            return VimAction(VimActionKind.APPEND_CHAR, key)

        return _NONE


# Singleton for the common "do nothing" action.
_NONE = VimAction(VimActionKind.NONE)
