"""Startup splash - BIOS-style, ultra-compact, minimal vertical space.

Everything fits on a small laptop screen. Dense monospace output like
a computer booting up - no wasted lines, no empty gaps.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.console import Console

# ── Compact one-line logo ──────────────────────────────────────
LOGO_INLINE = "[bold]BERNSTEIN[/bold] [dim]v{version}[/dim]"

# Use ASCII-safe separator on Windows to avoid cp1252 encoding issues
_SEP_CHAR = "-" if sys.platform == "win32" else "─"


def _detect_terminal_width(console: Console) -> int:
    """Get terminal width, default 80."""
    try:
        return console.size.width
    except Exception:
        return 80


def _block_logo_lines() -> list[str]:
    """The multi-line block-art BERNSTEIN logo, or ``[]`` when unavailable.

    Reuses the same packaged asset the premium splash renders so both tiers
    show the identical wordmark. ``_load_logo`` returns a single-line text
    fallback (``["  BERNSTEIN"]``) when the asset is missing; only the real
    multi-line art is worth drawing here, so a shorter result yields ``[]``.
    """
    try:
        from bernstein.cli.display.splash_v2 import _load_logo

        lines = _load_logo()
    except Exception:
        return []
    return lines if len(lines) > 1 else []


def _print_block_logo(console: Console, width: int) -> None:
    """Draw the block-art logo when the terminal can actually show it.

    Gated on an interactive terminal wide enough to hold the art: non-TTY
    output (CI logs, pipes) and narrow terminals keep the dense one-liner so
    scrollback and captured logs stay compact.
    """
    if not console.is_terminal:
        return
    lines = _block_logo_lines()
    if not lines:
        return
    logo_w = max(len(line) for line in lines)
    if width < logo_w + 2:
        return

    from rich.text import Text

    pad = max(0, (min(width, 80) - logo_w) // 2)
    for line in lines:
        console.print(Text(" " * pad + line, style="bold cyan"))


def _print_agents_block(
    console: Console,
    agents: list[dict[str, Any]],
    is_animated: bool,
) -> None:
    """Print agent probe lines in BIOS style."""
    parts: list[str] = []
    for a in agents:
        name = a.get("name", "?")
        authed = a.get("logged_in", False)
        model = a.get("default_model", "")
        short_model = model.split("-")[-1] if model else "?"
        icon = "[green]ok[/green]" if authed else "[dim]--[/dim]"
        parts.append(f"{icon} {name}[dim]/{short_model}[/dim]")

        if is_animated:
            console.print(f"  [dim]probe[/dim] {name:<8} {icon} [dim]{model}[/dim]")
            time.sleep(0.04)

    if not is_animated:
        console.print("  [dim]agents[/dim] " + "  ".join(parts))


def _print_status_lines(
    console: Console,
    *,
    width: int,
    seed_file: str | None,
    goal_preview: str,
    budget: float,
    task_count: int,
) -> None:
    """Print seed/goal/task/budget status lines."""
    if seed_file:
        console.print(f"  [dim]seed[/dim]   {seed_file}")
    if goal_preview:
        g = goal_preview[: min(60, width - 12)]
        console.print(f"  [dim]goal[/dim]   {g}")
    if task_count > 0:
        extra = f"  [dim]budget ${budget:.2f}[/dim]" if budget > 0 else ""
        console.print(f"  [dim]tasks[/dim]  {task_count}{extra}")
    elif budget > 0:
        console.print(f"  [dim]budget[/dim] ${budget:.2f}")


def splash(
    console: Console,
    *,
    version: str = "",
    agents: list[dict[str, Any]] | None = None,
    seed_file: str | None = None,
    goal_preview: str = "",
    budget: float = 0.0,
    task_count: int = 0,
    skip_animation: bool = False,
) -> None:
    """Show the startup splash: BIOS-style, compact, all in one block."""
    width = _detect_terminal_width(console)
    is_animated = not skip_animation and console.is_terminal

    sep = f"[dim]{_SEP_CHAR}[/dim]" * min(56, width - 2)
    console.print(sep)
    _print_block_logo(console, width)
    ver = f" v{version}" if version else ""
    console.print(f"  [bold blue]BERNSTEIN[/bold blue][dim]{ver}  declarative agent orchestration[/dim]")
    console.print(sep)

    if agents:
        _print_agents_block(console, agents, is_animated)

    _print_status_lines(
        console,
        width=width,
        seed_file=seed_file,
        goal_preview=goal_preview,
        budget=budget,
        task_count=task_count,
    )

    console.print(sep)
