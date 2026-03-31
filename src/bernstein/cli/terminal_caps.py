"""Terminal capability detection for graphics protocol selection.

Inspects environment variables and terminal state to determine the best
available image rendering protocol. Immutable, cacheable dataclass — construct
once per process with :func:`detect_capabilities` (cached) or
:meth:`TerminalCaps.detect` (uncached, for testing).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from enum import StrEnum


class Protocol(StrEnum):
    """Available image rendering protocols, ordered best -> fallback."""

    KITTY = "kitty"
    ITERM2 = "iterm2"
    SIXEL = "sixel"
    HALF_BLOCK = "half_block"
    BRAILLE = "braille"
    ASCII = "ascii"
    NONE = "none"


@dataclass(frozen=True)
class TerminalCaps:
    """Detected terminal rendering capabilities.

    Construct with :func:`detect_capabilities` (cached) from the live
    environment, or supply explicit values in unit tests.

    Attributes:
        is_tty: Whether stdout is connected to an interactive terminal.
        supports_truecolor: Terminal accepts 24-bit (16 M color) ANSI codes.
        supports_256color: Terminal accepts 256-color ANSI codes.
        supports_kitty: Kitty graphics protocol (APC-based) is available.
        supports_iterm2: iTerm2 OSC 1337 inline images are supported.
        supports_sixel: DCS sixel encoding is supported.
        term_width: Terminal width in columns (default 80 when unknown).
        term_height: Terminal height in rows (default 24 when unknown).
    """

    is_tty: bool
    supports_truecolor: bool
    supports_256color: bool
    supports_kitty: bool
    supports_iterm2: bool
    supports_sixel: bool
    term_width: int
    term_height: int

    # ── New capability aliases (all False on non-TTY) ──────────────────────

    @property
    def kitty_graphics(self) -> bool:
        """Kitty APC graphics protocol is available."""
        return self.is_tty and self.supports_kitty

    @property
    def iterm2_inline(self) -> bool:
        """iTerm2 OSC 1337 inline images are supported."""
        return self.is_tty and self.supports_iterm2

    @property
    def sixel(self) -> bool:
        """DCS sixel encoding is supported."""
        return self.is_tty and self.supports_sixel

    @property
    def truecolor(self) -> bool:
        """24-bit ANSI color is supported."""
        return self.is_tty and self.supports_truecolor

    @property
    def halfblocks(self) -> bool:
        """Unicode half-block characters are usable (requires color + TTY)."""
        return self.is_tty and (self.supports_truecolor or self.supports_256color)

    @property
    def sync_output(self) -> bool:
        """Synchronized output mode 2026 is safe to emit (TTY only)."""
        return self.is_tty

    @property
    def braille(self) -> bool:
        """Unicode Braille patterns are available (TTY only)."""
        return self.is_tty

    # ── Classmethod constructors ───────────────────────────────────────────

    @classmethod
    def detect(cls) -> TerminalCaps:
        """Detect capabilities from the current process environment.

        Uses environment variables only (no terminal query round-trips), which
        keeps detection instant and safe for use at import time.  Uncached —
        use :func:`detect_capabilities` for a cached, production-safe call.
        """
        term = os.environ.get("TERM", "")
        term_program = os.environ.get("TERM_PROGRAM", "")
        term_program_lower = term_program.lower()
        colorterm = os.environ.get("COLORTERM", "").lower()

        truecolor = colorterm in ("truecolor", "24bit")
        color256 = "256color" in term or truecolor

        # Kitty: native env var, WezTerm (full Kitty support), Ghostty
        kitty = (
            bool(os.environ.get("KITTY_WINDOW_ID"))
            or term_program_lower == "wezterm"
            or term_program_lower == "ghostty"
        )

        # iTerm2: native iTerm2, WezTerm, VS Code, Konsole
        iterm2 = (
            "iterm" in term_program_lower
            or term_program_lower == "wezterm"
            or term_program_lower == "vscode"
            or bool(os.environ.get("KONSOLE_VERSION"))
        )

        # Sixel: WezTerm, xterm, VS Code (1.80+), Windows Terminal (1.23+),
        #        Konsole (22.04+), foot
        sixel = (
            term_program_lower == "wezterm"
            or term in ("xterm-256color", "xterm")
            or term_program_lower == "vscode"
            or bool(os.environ.get("WT_SESSION"))
            or bool(os.environ.get("KONSOLE_VERSION"))
        )

        try:
            size = shutil.get_terminal_size()
            w, h = size.columns, size.lines
        except Exception:
            w, h = 80, 24

        return cls(
            is_tty=sys.stdout.isatty(),
            supports_truecolor=truecolor,
            supports_256color=color256,
            supports_kitty=kitty,
            supports_iterm2=iterm2,
            supports_sixel=sixel,
            term_width=w,
            term_height=h,
        )

    @classmethod
    def null(cls) -> TerminalCaps:
        """Minimal caps — no color, no graphics (CI / pipe / dumb terminal)."""
        return cls(
            is_tty=False,
            supports_truecolor=False,
            supports_256color=False,
            supports_kitty=False,
            supports_iterm2=False,
            supports_sixel=False,
            term_width=80,
            term_height=24,
        )

    # ── Protocol selection ─────────────────────────────────────────────────

    @property
    def best_protocol(self) -> Protocol:
        """Return the highest-fidelity available protocol.

        Priority: Kitty → iTerm2 → Sixel → Half-block → Braille → None.
        Returns Protocol.NONE when stdout is not a TTY.
        """
        if not self.is_tty:
            return Protocol.NONE
        if self.supports_kitty:
            return Protocol.KITTY
        if self.supports_iterm2:
            return Protocol.ITERM2
        if self.supports_sixel:
            return Protocol.SIXEL
        if self.supports_truecolor or self.supports_256color:
            return Protocol.HALF_BLOCK
        return Protocol.BRAILLE

    @property
    def best_image_protocol(self) -> Protocol:
        """Alias for :attr:`best_protocol` — the highest-fidelity available protocol."""
        return self.best_protocol


# ── Module-level cached detection ─────────────────────────────────────────

_caps_cache: TerminalCaps | None = None


def detect_capabilities() -> TerminalCaps:
    """Detect terminal capabilities, cached after first call.

    On non-TTY environments (CI, pipes, redirected output) all capability
    flags are False — safe to call unconditionally.

    Returns:
        Frozen :class:`TerminalCaps` with detected capability flags.
    """
    global _caps_cache
    if _caps_cache is None:
        _caps_cache = TerminalCaps.detect()
    return _caps_cache
