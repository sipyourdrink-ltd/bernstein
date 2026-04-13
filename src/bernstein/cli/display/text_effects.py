"""TerminalTextEffects integration for Bernstein CLI animations.

Wraps TTE (pure Python, zero deps, 37+ effects at 60fps) for animated
text reveals and typing effects in the Bernstein splash sequence.

Public API:
    logo_reveal(text, effect="beams", colors=...)  — BERNSTEIN logo reveal
    typing_effect(lines, speed=0.03)               — boot message typing

Both functions degrade gracefully:
- Non-TTY / CI: plain print, no animation
- TTE not installed: plain print, no animation
- Any keypress during animation: skip to final state
"""

from __future__ import annotations

import importlib
import select
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

# ---------------------------------------------------------------------------
# Color palette (matches splash_screen.py)
# ---------------------------------------------------------------------------

DEFAULT_COLORS: list[str] = ["#00ff41", "#00d4ff"]

# Map friendly names → (module_path, class_name)
_EFFECT_MAP: dict[str, tuple[str, str]] = {
    "beams": ("terminaltexteffects.effects.effect_beams", "Beams"),
    "decrypt": ("terminaltexteffects.effects.effect_decrypt", "Decrypt"),
    "laser": ("terminaltexteffects.effects.effect_laseretch", "LaserEtch"),
    "spray": ("terminaltexteffects.effects.effect_spray", "Spray"),
}

# ---------------------------------------------------------------------------
# Terminal capability helpers
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    """Return True when stdout is a real interactive terminal."""
    return sys.stdout.isatty()


def _tte_available() -> bool:
    """Return True when terminaltexteffects is importable."""
    try:
        importlib.import_module("terminaltexteffects")
        return True
    except ImportError:
        return False


def _key_pressed() -> bool:
    """Non-blocking check — True if any stdin input is waiting."""
    if not sys.stdin.isatty():
        return False
    try:
        return bool(select.select([sys.stdin], [], [], 0.0)[0])
    except (OSError, ValueError):
        return False


def _drain_stdin() -> None:
    """Consume buffered stdin so keypresses don't leak to the shell prompt."""
    if not sys.stdin.isatty():
        return
    try:
        while select.select([sys.stdin], [], [], 0.0)[0]:
            sys.stdin.read(1)
    except (OSError, ValueError):
        pass  # stdin not readable; skip buffer drain


# ---------------------------------------------------------------------------
# Color conversion
# ---------------------------------------------------------------------------


def _strip_hash(color: str) -> str:  # pyright: ignore[reportUnusedFunction]
    """Convert ``#rrggbb`` → ``rrggbb`` for TTE color strings."""
    return color.lstrip("#")


# ---------------------------------------------------------------------------
# TTE effect runner
# ---------------------------------------------------------------------------


def _load_effect_class(effect_name: str) -> type[Any] | None:
    """Return the TTE effect class for *effect_name*, or None on failure.

    Falls back to Beams if the requested effect cannot be imported.
    """
    module_path, class_name = _EFFECT_MAP.get(
        effect_name,
        _EFFECT_MAP["beams"],
    )
    try:
        mod = importlib.import_module(module_path)
        cls: type[Any] = getattr(mod, class_name)
        return cls
    except (ImportError, AttributeError):
        if effect_name != "beams":
            return _load_effect_class("beams")
        return None


def _run_tte_reveal(text: str, effect_name: str) -> bool:
    """Run a TTE animation.  Returns True if animation completed, False on skip/error.

    On keypress the animation breaks early; the caller is responsible for
    printing the final plain text.
    """
    cls = _load_effect_class(effect_name)
    if cls is None:
        return False

    try:
        effect = cls(text)
        with effect.terminal_output() as terminal:  # type: ignore[attr-defined]
            for frame in effect:
                if _key_pressed():
                    return False
                terminal.print(frame)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def logo_reveal(
    text: str,
    effect: str = "beams",
    colors: list[str] | None = None,
) -> None:
    """Animate a text reveal using TerminalTextEffects.

    Degrades to a plain ``print`` in non-TTY environments (CI, pipes) or
    when *terminaltexteffects* is not installed.  Any keypress during the
    animation skips to the final state.  Total duration is under 2 seconds.

    Args:
        text: Text to animate/display (e.g. ``"BERNSTEIN"``).
        effect: Effect name — ``"beams"`` (default), ``"decrypt"``,
            ``"laser"`` (LaserEtch), or ``"spray"``.  Unknown names fall
            back to ``"beams"``.
        colors: Hex color strings, e.g. ``["#00ff41", "#00d4ff"]``.
            Defaults to Bernstein green/cyan palette.  Passed to the effect
            when the TTE API supports it; ignored silently otherwise.
    """
    # colors parameter reserved for future TTE color customization;
    # currently unused by _run_tte_reveal.
    _ = colors or DEFAULT_COLORS

    if not _is_tty() or not _tte_available():
        print(text)
        return

    completed = _run_tte_reveal(text, effect)
    if not completed:
        # Skipped or errored — print final state
        _drain_stdin()
        print(text)


def typing_effect(lines: list[str], speed: float = 0.03) -> None:
    """Display *lines* with a character-by-character typing animation.

    Prints each character with a *speed*-second delay to simulate live
    typing.  Degrades to instant ``print`` in non-TTY environments.
    Any keypress flushes remaining text immediately.

    Args:
        lines: Lines of text to display in order.
        speed: Seconds between characters (default 0.03 = 30 ms).
    """
    if not _is_tty():
        for line in lines:
            print(line)
        return

    all_skipped = False

    for line in lines:
        if all_skipped:
            print(line)
            continue

        for i, char in enumerate(line):
            if _key_pressed():
                # Flush rest of this line instantly, then all remaining lines
                sys.stdout.write(line[i:])
                sys.stdout.write("\n")
                sys.stdout.flush()
                _drain_stdin()
                all_skipped = True
                break
            sys.stdout.write(char)
            sys.stdout.flush()
            time.sleep(speed)

        if not all_skipped:
            sys.stdout.write("\n")
            sys.stdout.flush()
