"""BBS-style boot sequence animation for Bernstein TUI.

Plays a ~3-second retro modem-handshake + ANSI art animation before the
interactive TUI starts, evoking the ACiD/iCE BBS demoscene aesthetic of the
late '80s and early '90s.
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.style import Style
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import Console

# ---------------------------------------------------------------------------
# ANSI art logo — ACiD/iCE-era block characters with shadow
# ---------------------------------------------------------------------------

ANSI_LOGO: str = r"""
░▒▓█                                                                      █▓▒░
░▒▓█  ██████╗ ███████╗██████╗ ███╗   ██╗███████╗████████╗███████╗██╗███╗  █▓▒░
░▒▓█  ██╔══██╗██╔════╝██╔══██╗████╗  ██║██╔════╝╚══██╔══╝██╔════╝██║████╗ █▓▒░
░▒▓█  ██████╔╝█████╗  ██████╔╝██╔██╗ ██║███████╗   ██║   █████╗  ██║█╔██╗█▓▒░
░▒▓█  ██╔══██╗██╔══╝  ██╔══██╗██║╚██╗██║╚════██║   ██║   ██╔══╝  ██║█║╚██▓▒░
░▒▓█  ██████╔╝███████╗██║  ██║██║ ╚████║███████║   ██║   ███████╗██║█║ ╚█▒░
░▒▓█  ╚═════╝ ╚══════╝╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝   ╚═╝   ╚══════╝╚═╝╚═╝ ░
░▒▓█                                                                      █▓▒░
░▒▓█▓▒░━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━░▒▓█▓▒░
░▒▓█              A G E N T   O R C H E S T R A   v 1 . 4              █▓▒░
░▒▓█▓▒░━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━░▒▓█▓▒░
""".strip("\n")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_agents(project_dir: Path) -> int:
    """Count agents from ``.sdd/runtime/agents.json``.

    Returns 0 when the file is missing or unreadable.
    """
    agents_file = project_dir / ".sdd" / "runtime" / "agents.json"
    try:
        data = cast("object", json.loads(agents_file.read_text(encoding="utf-8")))
        if isinstance(data, list):
            return len(cast("list[object]", data))
        if isinstance(data, dict):
            return len(cast("dict[str, object]", data))
        return 0
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 0


def _count_backlog(project_dir: Path) -> int:
    """Count YAML files in ``.sdd/backlog/``.

    Returns 0 when the directory is missing.
    """
    backlog_dir = project_dir / ".sdd" / "backlog"
    try:
        return sum(1 for f in backlog_dir.iterdir() if f.suffix in {".yaml", ".yml"})
    except (FileNotFoundError, OSError):
        return 0


async def _check_skip() -> bool:
    """Non-blocking check for keypress (skip signal).

    Uses ``select.select`` on Unix to detect pending stdin input without
    blocking the event loop.  Returns ``True`` when input is available
    (meaning the user pressed a key to skip).
    """
    try:
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if readable:
            sys.stdin.read(1)  # consume the character
            return True
    except (OSError, ValueError):
        # stdin may not be selectable (e.g. piped, Windows)
        pass
    return False


async def _typewriter(
    console: Console,
    text: str,
    style: str,
    delay_ms: int = 50,
) -> None:
    """Print *text* character by character with *delay_ms* between each.

    Args:
        console: Rich Console to write to.
        text: The string to type out.
        style: Rich style string applied to every character.
        delay_ms: Milliseconds between each character.
    """
    for ch in text:
        console.print(ch, style=style, end="", highlight=False)
        await asyncio.sleep(delay_ms / 1000)
    console.print()  # newline


async def _animate_dots(console: Console, count: int = 12, delay_ms: int = 20) -> None:
    """Print dots one at a time, used for the system-check lines."""
    for _ in range(count):
        console.print(".", style="green", end="", highlight=False)
        await asyncio.sleep(delay_ms / 1000)


# ---------------------------------------------------------------------------
# Main sequence
# ---------------------------------------------------------------------------


async def play_boot_sequence(
    console: Console,
    *,
    no_splash: bool = False,
    project_dir: Path | None = None,
) -> None:
    """Play the BBS-style boot animation.

    Args:
        console: Rich Console instance to render to.
        no_splash: Skip the entire sequence.
        project_dir: Project directory for reading real agent/task counts.
    """
    if no_splash or os.environ.get("BERNSTEIN_NO_SPLASH") == "1":
        return

    proj = project_dir or Path(".")
    skipped = False

    async def _maybe_skip() -> bool:
        nonlocal skipped
        if skipped:
            return True
        if await _check_skip():
            skipped = True
            return True
        return False

    # 1. Clear screen, hide cursor
    console.print("\033[2J\033[H", end="", highlight=False)  # ANSI clear + home
    console.print("\033[?25l", end="", highlight=False)  # hide cursor

    try:
        # 2. Modem handshake
        modem_commands: list[tuple[str, str, int, bool]] = [
            ("ATZ", "green", 50, True),
            ("OK", "bold bright_green", 0, False),
            ("ATDT 127.0.0.1:8052", "green", 50, True),
            ("CONNECT 14400", "bold bright_green", 0, False),
            ("", "green", 0, False),
            ("CARRIER DETECT", "bold bright_green", 0, False),
        ]

        for text, style, delay, is_typed in modem_commands:
            if await _maybe_skip():
                break
            if is_typed:
                await _typewriter(console, text, style, delay_ms=delay)
                await asyncio.sleep(0.2)
            elif text:
                if text == "CARRIER DETECT":
                    await asyncio.sleep(0.3)
                else:
                    await asyncio.sleep(0.2)
                console.print(text, style=style, highlight=False)
            else:
                console.print(highlight=False)

        if await _maybe_skip():
            return

        # Brief pause before logo
        await asyncio.sleep(0.2)

        # 3. ANSI art logo — line by line reveal
        logo_style = Style(color="bright_yellow")
        for line in ANSI_LOGO.splitlines():
            if await _maybe_skip():
                break
            styled_line = Text(line, style=logo_style)
            console.print(styled_line, highlight=False)
            await asyncio.sleep(0.03)

        if await _maybe_skip():
            return

        console.print(highlight=False)
        await asyncio.sleep(0.2)

        # 4. System checks
        agent_count = _count_agents(proj)
        backlog_count = _count_backlog(proj)

        checks: list[tuple[str, str]] = [
            ("Scanning agents", f" {agent_count} found" if agent_count else " 0 found"),
            ("Loading backlog", f" {backlog_count} tasks" if backlog_count else " 0 tasks"),
            ("Task server", " ONLINE"),
            ("Initializing orchestra", " OK"),
        ]

        for label, result in checks:
            if await _maybe_skip():
                break
            console.print(label, style="green", end="", highlight=False)
            await _animate_dots(console)
            console.print(result, style="bold bright_green", highlight=False)
            await asyncio.sleep(0.15)

        if await _maybe_skip():
            return

        await asyncio.sleep(0.1)

        # 5. CRT power-on flash
        width = console.width or 80
        flash_line = " " * width
        console.print("\033[2J\033[H", end="", highlight=False)
        for _ in range(min(console.height or 24, 24)):
            console.print(flash_line, style="on white", end="", highlight=False)
        await asyncio.sleep(0.05)
        console.print("\033[2J\033[H", end="", highlight=False)

    finally:
        # Always restore cursor
        console.print("\033[?25h", end="", highlight=False)
