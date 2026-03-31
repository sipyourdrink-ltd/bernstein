"""Terminal capability detection for graphics protocol selection.

Inspects environment variables and terminal state to determine the best
available image rendering protocol. Immutable, cacheable dataclass — construct
once per process with :meth:`TerminalCaps.detect`.
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
    NONE = "none"


@dataclass(frozen=True)
class TerminalCaps:
    """Detected terminal rendering capabilities.

    Construct with :meth:`detect` from the live environment, or supply
    explicit values in unit tests.

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

    # ── Classmethod constructors ───────────────────────────────────────────

    @classmethod
    def detect(cls) -> TerminalCaps:
        """Detect capabilities from the current process environment.

        Uses environment variables only (no terminal query round-trips), which
        keeps detection instant and safe for use at import time.
        """
        term = os.environ.get("TERM", "")
        term_program = os.environ.get("TERM_PROGRAM", "")
        colorterm = os.environ.get("COLORTERM", "").lower()

        truecolor = colorterm in ("truecolor", "24bit")
        color256 = "256color" in term or truecolor

        # Kitty: native env var, or WezTerm (full Kitty protocol support)
        kitty = bool(os.environ.get("KITTY_WINDOW_ID")) or term_program == "WezTerm"

        # iTerm2: native iTerm2, plus WezTerm which supports OSC 1337
        iterm2 = "iTerm" in term_program or term_program == "WezTerm"

        # Sixel: WezTerm, xterm-256color (a common sixel-capable config)
        sixel = term_program == "WezTerm" or term in ("xterm-256color",)

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
