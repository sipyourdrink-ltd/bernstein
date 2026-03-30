"""Startup splash — compact, animated, demoscene-inspired.

Replaces the verbose multi-screen startup with a tight animated
sequence that feels fast and intentional.  Total duration: <2s.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from rich.live import Live
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import Console

# ── ASCII logo ──────────────────────────────────────────────────
# Compact 5-line logo. No emoji, no box-drawing — just clean type.
LOGO = r"""
 ▄▄▄▄    ▓█████  ██▀███   ███▄    █   ██████ ▄▄▄█████▓▓█████  ██▓ ███▄    █
▓█████▄  ▓█   ▀ ▓██ ▒ ██▒ ██ ▀█   █ ▒██    ▒ ▓  ██▒ ▓▒▓█   ▀ ▓██▒ ██ ▀█   █
▒██▒ ▄██▒▒███   ▓██ ░▄█ ▒▓██  ▀█ ██▒░ ▓██▄   ▒ ▓██░ ▒░▒███   ▒██▒▓██  ▀█ ██▒
▒██░█▀   ▒▓█  ▄ ▒██▀▀█▄  ▓██▒  ▐▌██▒  ▒   ██▒░ ▓██▓ ░ ▒▓█  ▄ ░██░▓██▒  ▐▌██▒
░▓█  ▀█▓ ░▒████▒░██▓ ▒██▒▒██░   ▓██░▒██████▒▒  ▒██▒ ░ ░▒████▒░██░▒██░   ▓██░
 ▒▓███▀▒ ░░ ▒░ ░░ ▒▓ ░▒▓░░ ▒░   ▒ ▒ ▒ ▒▓▒ ▒ ░  ▒ ░░   ░░ ▒░ ░░▓  ░ ▒░   ▒ ▒
"""

LOGO_SMALL = """\
╔══════════════════════════════════╗
║     BERNSTEIN — v{version:<14s}║
║     multi-agent orchestration    ║
╚══════════════════════════════════╝"""


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
    """Show the startup splash screen.

    Args:
        console: Rich console instance.
        version: Package version string.
        agents: Detected agent capabilities (from auto-detection).
        seed_file: Path to seed file if found.
        goal_preview: First 80 chars of the goal.
        budget: Budget cap in USD.
        task_count: Number of tasks in backlog.
        skip_animation: If True, print static (for CI/piped output).
    """
    width = _detect_terminal_width(console)
    use_big_logo = width >= 90

    if skip_animation or not console.is_terminal:
        _print_static(console, version, agents, seed_file, goal_preview, budget, task_count, use_big_logo)
        return

    _print_animated(console, version, agents, seed_file, goal_preview, budget, task_count, use_big_logo)


def _print_static(
    console: Console,
    version: str,
    agents: list[dict[str, Any]] | None,
    seed_file: str | None,
    goal_preview: str,
    budget: float,
    task_count: int,
    use_big_logo: bool,
) -> None:
    """Non-animated fallback for CI/piped output."""
    if use_big_logo:
        console.print(f"[bold blue]{LOGO}[/bold blue]")
    else:
        console.print(f"[bold blue]{LOGO_SMALL.format(version=version or '?')}[/bold blue]")

    if agents:
        names = ", ".join(a.get("name", "?") for a in agents)
        console.print(f"  [green]agents:[/green] {names}")
    if seed_file:
        console.print(f"  [green]seed:[/green]   {seed_file}")
    if goal_preview:
        console.print(f"  [green]goal:[/green]   {goal_preview[:76]}...")
    if budget > 0:
        console.print(f"  [green]budget:[/green] ${budget:.2f}")
    if task_count > 0:
        console.print(f"  [green]tasks:[/green]  {task_count}")
    console.print()


def _print_animated(
    console: Console,
    version: str,
    agents: list[dict[str, Any]] | None,
    seed_file: str | None,
    goal_preview: str,
    budget: float,
    task_count: int,
    use_big_logo: bool,
) -> None:
    """Animated startup — logo fade-in + scanning effect."""

    # Phase 1: Logo (instant)
    if use_big_logo:
        console.print(f"[bold blue]{LOGO}[/bold blue]")
    else:
        console.print(f"[bold blue]{LOGO_SMALL.format(version=version or '?')}[/bold blue]")

    # Phase 2: Scanning effect for agent detection
    scan_items: list[tuple[str, str]] = []
    if agents:
        for a in agents:
            name = a.get("name", "?")
            authed = a.get("logged_in", False)
            icon = "[green]✓[/green]" if authed else "[dim]○[/dim]"
            model = a.get("default_model", "")
            scan_items.append((f"  {icon} {name}", f"[dim]{model}[/dim]"))

    status_items: list[tuple[str, str]] = []
    if seed_file:
        status_items.append(("  seed", seed_file))
    if goal_preview:
        status_items.append(("  goal", goal_preview[:72]))
    if budget > 0:
        status_items.append(("  budget", f"${budget:.2f}"))
    if task_count > 0:
        status_items.append(("  tasks", str(task_count)))

    # Animate: reveal each line with a short delay
    with Live(Text(""), console=console, refresh_per_second=30, transient=True) as live:
        lines: list[str] = []

        # Agent scan
        if scan_items:
            lines.append("  [bold]agents[/bold]")
            live.update(Text.from_markup("\n".join(lines)))
            time.sleep(0.05)

            for label, detail in scan_items:
                lines.append(f"{label} {detail}")
                live.update(Text.from_markup("\n".join(lines)))
                time.sleep(0.08)  # 80ms per agent — fast but visible

        # Status lines
        if status_items:
            lines.append("")
            for label, value in status_items:
                lines.append(f"  [bold]{label}[/bold]  {value}")
                live.update(Text.from_markup("\n".join(lines)))
                time.sleep(0.05)

        lines.append("")
        live.update(Text.from_markup("\n".join(lines)))
        time.sleep(0.1)

    # Print final state (non-transient)
    for line in lines:
        if line:
            console.print(line)

    # Separator
    console.print("[dim]─[/dim]" * min(60, _detect_terminal_width(console)))
    console.print()
