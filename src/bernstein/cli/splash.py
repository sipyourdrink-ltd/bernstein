"""Startup splash — BIOS-style, ultra-compact, minimal vertical space.

Everything fits on a small laptop screen. Dense monospace output like
a computer booting up — no wasted lines, no empty gaps.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.console import Console

# ── Compact one-line logo ──────────────────────────────────────
LOGO_INLINE = "[bold]BERNSTEIN[/bold] [dim]v{version}[/dim]"


def _detect_terminal_width(console: Console) -> int:
    """Get terminal width, default 80."""
    try:
        return console.size.width
    except Exception:
        return 80


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
    """Show the startup splash — BIOS-style, compact, all in one block."""
    width = _detect_terminal_width(console)
    is_animated = not skip_animation and console.is_terminal

    # ── Header line ──
    sep = "[dim]─[/dim]" * min(56, width - 2)
    console.print(sep)
    ver = f" v{version}" if version else ""
    console.print(f"  [bold blue]BERNSTEIN[/bold blue][dim]{ver}  declarative agent orchestration[/dim]")
    console.print(sep)

    # ── Agents (single dense block, no header) ──
    if agents:
        parts: list[str] = []
        for a in agents:
            name = a.get("name", "?")
            authed = a.get("logged_in", False)
            model = a.get("default_model", "")
            short_model = model.split("-")[-1] if model else "?"
            icon = "[green]ok[/green]" if authed else "[dim]--[/dim]"
            parts.append(f"{icon} {name}[dim]/{short_model}[/dim]")

            if is_animated:
                # Print agents as they're "detected" — fast BIOS-style
                console.print(f"  [dim]probe[/dim] {name:<8} {icon} [dim]{model}[/dim]")
                time.sleep(0.04)

        if not is_animated:
            # Static: all agents on one line
            console.print("  [dim]agents[/dim] " + "  ".join(parts))

    # ── Status (all on minimal lines) ──
    if seed_file:
        console.print(f"  [dim]seed[/dim]   {seed_file}")
    if goal_preview:
        g = goal_preview[:min(60, width - 12)]
        console.print(f"  [dim]goal[/dim]   {g}")
    if task_count > 0:
        extra = f"  [dim]budget ${budget:.2f}[/dim]" if budget > 0 else ""
        console.print(f"  [dim]tasks[/dim]  {task_count}{extra}")
    elif budget > 0:
        console.print(f"  [dim]budget[/dim] ${budget:.2f}")

    console.print(sep)
