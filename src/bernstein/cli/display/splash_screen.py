"""Futuristic boot-sequence splash screen -- BIOS POST meets sci-fi terminal.

Full-screen takeover using Rich Live display with three phases:
  1. Boot POST -- system identification and hardware checks
  2. Agent Detection -- animated probe of installed CLI agents
  3. System Ready -- progress bar fill + status line

Total duration: ~2-3 seconds, skippable with any keypress.
Non-TTY safe: silently skips when stdout is piped or in CI.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import time
from contextlib import contextmanager

# Platform-specific imports for terminal input handling
if sys.platform == "win32":
    import msvcrt

    _HAS_TERMIOS = False
else:
    import select
    import termios
    import tty

    _HAS_TERMIOS = True
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.live import Live
from rich.text import Text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rich.console import Console

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

C_GREEN = "#00ff41"
C_CYAN = "#00d4ff"
C_DIM = "#555555"
C_WARN = "#ffaa00"
C_ERR = "#ff3333"
C_WHITE = "#cccccc"
C_BRIGHT = "#ffffff"

# ---------------------------------------------------------------------------
# ASCII logo (fallback for non-Sixel terminals)
# ---------------------------------------------------------------------------

ASCII_LOGO = r"""
 ____  _____ ____  _   _ ____ _____ _____ ___ _   _
| __ )| ____|  _ \| \ | / ___|_   _| ____|_ _| \ | |
|  _ \|  _| | |_) |  \| \___ \ | | |  _|  | ||  \| |
| |_) | |___|  _ <| |\  |___) || | | |___ | || |\  |
|____/|_____|_| \_\_| \_|____/ |_| |_____|___|_| \_|
"""

# Compact version for narrow terminals (< 60 cols)
ASCII_LOGO_COMPACT = r"""
 BERNSTEIN
"""

# ---------------------------------------------------------------------------
# Non-blocking keypress detection
# ---------------------------------------------------------------------------


@contextmanager
def _raw_mode() -> Iterator[None]:
    """Put stdin in raw mode so we can detect keypresses without blocking.

    Restores original terminal settings on exit. Silently no-ops when stdin
    is not a real TTY (pipes, CI, etc.).
    """
    if not sys.stdin.isatty():
        yield
        return

    if sys.platform == "win32":
        # Windows doesn't need raw mode for msvcrt.kbhit()
        yield
        return

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _key_pressed() -> bool:
    """Check if a key has been pressed without blocking.

    Returns True if any input is available on stdin.
    """
    if not sys.stdin.isatty():
        return False

    if sys.platform == "win32":
        try:
            return msvcrt.kbhit()
        except (OSError, ValueError):
            return False

    try:
        return bool(select.select([sys.stdin], [], [], 0.0)[0])
    except (OSError, ValueError):
        return False


def _drain_input() -> None:
    """Consume any buffered input so it doesn't leak to the next prompt."""
    if not sys.stdin.isatty():
        return

    if sys.platform == "win32":
        try:
            while msvcrt.kbhit():
                msvcrt.getch()
        except (OSError, ValueError):
            pass
        return

    try:
        while select.select([sys.stdin], [], [], 0.0)[0]:
            sys.stdin.read(1)
    except (OSError, ValueError):
        pass  # stdin not readable; skip buffer drain


# ---------------------------------------------------------------------------
# System info helpers
# ---------------------------------------------------------------------------


def _cpu_count() -> int:
    """Get CPU core count."""
    return os.cpu_count() or 4


def _memory_gb() -> int:
    """Get approximate system memory in GB."""
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2,
            )
            if result.returncode == 0:
                return int(result.stdout.strip()) // (1024**3)
        elif sys.platform == "linux":
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024**3)  # type: ignore[operator]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass  # Memory detection failed; use fallback
    return 16  # fallback


def _os_label() -> str:
    """Get a compact OS label."""
    system = platform.system()
    if system == "Darwin":
        release = platform.mac_ver()[0]
        return f"macOS {release}" if release else "macOS"
    if system == "Linux":
        # Try to get distro name
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')[:30]
        except OSError:
            pass  # Cannot read /etc/os-release
        return "Linux"
    return system


# ---------------------------------------------------------------------------
# Boot data model
# ---------------------------------------------------------------------------


@dataclass
class AgentProbe:
    """Result of probing a single agent for the splash display."""

    name: str
    model: str
    status: str  # "ok", "warn", "fail"
    detail: str  # e.g. "authenticated", "no API key"


@dataclass
class BootData:
    """All data needed to render the splash screen."""

    version: str = ""
    agents: list[AgentProbe] = field(default_factory=list[AgentProbe])
    seed_file: str | None = None
    goal_preview: str = ""
    budget: float = 0.0
    task_count: int = 0
    cpu_cores: int = 0
    memory_gb: int = 0
    os_label: str = ""

    def __post_init__(self) -> None:
        if self.cpu_cores == 0:
            self.cpu_cores = _cpu_count()
        if self.memory_gb == 0:
            self.memory_gb = _memory_gb()
        if not self.os_label:
            self.os_label = _os_label()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _timestamp(elapsed: float) -> str:
    """Format elapsed time as a dim left-margin timestamp: [00.142s]"""
    return f"[{C_DIM}][{elapsed:06.3f}s][/{C_DIM}]"


def _ok_badge() -> str:
    """Green [  OK  ] badge."""
    return f"[bold {C_GREEN}]\\[  OK  ][/bold {C_GREEN}]"


def _warn_badge() -> str:
    """Yellow [ WARN ] badge."""
    return f"[bold {C_WARN}]\\[ WARN ][/bold {C_WARN}]"


def _fail_badge() -> str:
    """Red [ FAIL ] badge."""
    return f"[bold {C_ERR}]\\[ FAIL ][/bold {C_ERR}]"


def _badge_for(status: str) -> str:
    """Return the appropriate badge for a status string."""
    if status == "ok":
        return _ok_badge()
    if status == "warn":
        return _warn_badge()
    return _fail_badge()


def _progress_bar(fraction: float, width: int = 40) -> str:
    """Render a progress bar: [||||||||........] 52%"""
    filled = int(fraction * width)
    empty = width - filled
    pct = int(fraction * 100)
    bar_filled = "\u2588" * filled  # full block
    bar_empty = "\u2591" * empty  # light shade
    color = C_GREEN if fraction >= 1.0 else C_CYAN
    return f"[{color}]{bar_filled}[/{color}][{C_DIM}]{bar_empty}[/{C_DIM}] [{C_WHITE}]{pct:>3}%[/{C_WHITE}]"


def _box_top(width: int) -> str:
    """Top border of a box: +-- ... --+"""
    inner = "\u2500" * (width - 2)
    return f"[{C_DIM}]\u250c{inner}\u2510[/{C_DIM}]"


def _box_bottom(width: int) -> str:
    """Bottom border of a box."""
    inner = "\u2500" * (width - 2)
    return f"[{C_DIM}]\u2514{inner}\u2518[/{C_DIM}]"


def _center_text(text: str, width: int) -> str:
    """Center text within a given width (approximate, since Rich markup is invisible)."""
    # Strip markup for length calc (rough heuristic)
    plain = re.sub(r"\[.*?\]", "", text)
    padding = max(0, (width - len(plain)) // 2)
    return " " * padding + text


# ---------------------------------------------------------------------------
# Screen builder -- assembles lines for each animation frame
# ---------------------------------------------------------------------------


class SplashRenderer:
    """Builds the splash screen frame by frame.

    Each call to a phase method extends self.lines, and render() returns
    a Rich renderable of the full screen.
    """

    def __init__(self, data: BootData, width: int, height: int) -> None:
        self.data = data
        self.width = min(width, 100)  # cap for readability
        self.height = height
        self.lines: list[str] = []

    def add_line(self, line: str) -> None:
        """Append a line to the screen buffer."""
        self.lines.append(line)

    def add_blank(self, count: int = 1) -> None:
        """Add blank lines."""
        for _ in range(count):
            self.lines.append("")

    def render(self) -> Text:
        """Return a Rich Text renderable of all accumulated lines."""
        markup = "\n".join(self.lines)
        # Pad to fill screen height for full-screen feel
        current_lines = len(self.lines)
        if current_lines < self.height:
            markup += "\n" * (self.height - current_lines)
        return Text.from_markup(markup)

    # -- Phase 1: Boot POST --

    def phase1_header(self, _elapsed: float) -> None:
        """Render the system identification header."""
        w = self.width

        # Box-draw border
        self.add_line(_box_top(w))
        self.add_blank()

        # Logo (pick size based on width)
        if w >= 60:
            for line in ASCII_LOGO.strip().splitlines():
                self.add_line(_center_text(f"[bold {C_GREEN}]{line}[/bold {C_GREEN}]", w))
        else:
            self.add_line(_center_text(f"[bold {C_GREEN}]BERNSTEIN[/bold {C_GREEN}]", w))

        self.add_blank()

        # System ID line
        ver = self.data.version or "dev"
        id_line = f"[bold {C_CYAN}]ORCHESTRATION SYSTEM[/bold {C_CYAN}] [{C_DIM}]v{ver}[/{C_DIM}]"
        self.add_line(_center_text(id_line, w))
        self.add_line(_center_text(f"[{C_DIM}]Multi-Agent CLI Orchestrator[/{C_DIM}]", w))
        self.add_blank()
        self.add_line(f"[{C_DIM}]{'=' * min(w - 4, 72)}[/{C_DIM}]")
        self.add_blank()

    def phase1_hw_checks(self, elapsed: float) -> None:
        """Render hardware-style system checks."""
        ts = _timestamp(elapsed)
        d = self.data

        checks = [
            (f"Platform: {d.os_label}", "OK"),
            (f"CPU cores: {d.cpu_cores}", "OK"),
            (f"Memory: {d.memory_gb}GB", "OK"),
            ("Python: " + platform.python_version(), "OK"),
            ("Orchestrator port: 8052", "OK"),
        ]

        for label, status in checks:
            dots = "." * max(3, 48 - len(label))
            status_col = C_GREEN if status == "OK" else C_WARN
            self.add_line(
                f"  {ts}  [{C_WHITE}]{label}[/{C_WHITE}] "
                f"[{C_DIM}]{dots}[/{C_DIM}] "
                f"[bold {status_col}]{status}[/bold {status_col}]"
            )

        self.add_blank()

    # -- Phase 2: Agent detection --

    def phase2_header(self, elapsed: float) -> None:
        """Render the agent detection section header."""
        ts = _timestamp(elapsed)
        self.add_line(f"  {ts}  [{C_CYAN}]Scanning for CLI agents...[/{C_CYAN}]")
        self.add_blank()

    def phase2_agent(self, agent: AgentProbe, elapsed: float) -> None:
        """Render a single agent probe line."""
        ts = _timestamp(elapsed)
        badge = _badge_for(agent.status)

        # Agent name padded, model, detail
        name = agent.name
        model = agent.model
        detail = agent.detail

        # Truncate model for narrow terminals
        max_model_len = max(10, self.width - 55)
        if len(model) > max_model_len:
            model = model[: max_model_len - 1] + "\u2026"

        self.add_line(
            f"  {ts}  {badge} [{C_BRIGHT}]{name:<12}[/{C_BRIGHT}] "
            f"[{C_DIM}]{model:<24}[/{C_DIM}] "
            f"[{C_WHITE}]{detail}[/{C_WHITE}]"
        )

    # -- Phase 3: System ready --

    def phase3_progress(self, fraction: float, elapsed: float) -> None:
        """Render the progress bar at current fraction."""
        ts = _timestamp(elapsed)
        bar = _progress_bar(fraction, width=min(40, self.width - 30))
        self.add_line(f"  {ts}  {bar}")

    def phase3_status(self, elapsed: float) -> None:
        """Render the final status line."""
        d = self.data
        ts = _timestamp(elapsed)

        agent_count = sum(1 for a in d.agents if a.status == "ok")
        total_agents = len(d.agents)
        task_label = f"{d.task_count} tasks queued" if d.task_count > 0 else "ready"

        self.add_blank()
        self.add_line(
            f"  {ts}  [bold {C_GREEN}]SYSTEM ONLINE[/bold {C_GREEN}] "
            f"[{C_DIM}]\u2014[/{C_DIM}] "
            f"[{C_WHITE}]{agent_count}/{total_agents} agents ready, {task_label}[/{C_WHITE}]"
        )

        if d.goal_preview:
            goal = d.goal_preview[: min(60, self.width - 20)]
            self.add_line(f"  {'':>11}  [{C_DIM}]Goal: {goal}[/{C_DIM}]")

        self.add_blank()
        self.add_line(_box_bottom(self.width))
        self.add_blank()


# ---------------------------------------------------------------------------
# Animation driver
# ---------------------------------------------------------------------------


def _run_animated(console: Console, data: BootData) -> None:  # pyright: ignore[reportUnusedFunction]
    """Run the full animated splash sequence using Rich Live."""
    width = min(console.size.width, 120)
    height = console.size.height

    renderer = SplashRenderer(data, width, height)
    t0 = time.monotonic()

    def elapsed() -> float:
        return time.monotonic() - t0

    skipped = False

    def check_skip() -> bool:
        nonlocal skipped
        if skipped:
            return True
        if _key_pressed():
            _drain_input()
            skipped = True
            return True
        return False

    with _raw_mode():
        console.clear()

        with Live(
            renderer.render(),
            console=console,
            refresh_per_second=30,
            transient=True,
        ) as live:

            def update() -> None:
                live.update(renderer.render())

            # -- Phase 1: Boot POST (0.0s - 0.5s) --

            # Header appears instantly
            renderer.phase1_header(elapsed())
            update()
            if not check_skip():
                time.sleep(0.15)

            # Hardware checks appear one by one
            if not check_skip():
                # Build checks incrementally
                d = data
                checks = [
                    (f"Platform: {d.os_label}", "OK"),
                    (f"CPU cores: {d.cpu_cores}", "OK"),
                    (f"Memory: {d.memory_gb}GB", "OK"),
                    ("Python: " + platform.python_version(), "OK"),
                    ("Orchestrator port: 8052", "OK"),
                ]

                for label, status in checks:
                    if check_skip():
                        break
                    ts = elapsed()
                    dots = "." * max(3, 48 - len(label))
                    status_col = C_GREEN if status == "OK" else C_WARN
                    renderer.add_line(
                        f"  {_timestamp(ts)}  [{C_WHITE}]{label}[/{C_WHITE}] "
                        f"[{C_DIM}]{dots}[/{C_DIM}] "
                        f"[bold {status_col}]{status}[/bold {status_col}]"
                    )
                    update()
                    time.sleep(0.06)

                renderer.add_blank()
                update()

            # -- Phase 2: Agent detection (0.5s - 1.5s) --

            if not check_skip():
                renderer.phase2_header(elapsed())
                update()
                time.sleep(0.1)

            if not check_skip() and data.agents:
                for agent in data.agents:
                    if check_skip():
                        break
                    renderer.phase2_agent(agent, elapsed())
                    update()
                    # Slightly longer pause for OK agents (simulates auth check)
                    delay = 0.18 if agent.status == "ok" else 0.1
                    time.sleep(delay)

                renderer.add_blank()
                update()

            # -- Phase 3: System ready (animated progress bar) --

            if not check_skip():
                # Remove any previously added progress line to overwrite it
                progress_line_idx = len(renderer.lines)
                renderer.add_line("")  # placeholder for progress bar
                steps = 20
                for i in range(steps + 1):
                    if check_skip():
                        break
                    fraction = i / steps
                    ts = elapsed()
                    bar = _progress_bar(fraction, width=min(40, width - 30))
                    renderer.lines[progress_line_idx] = f"  {_timestamp(ts)}  {bar}"
                    update()
                    time.sleep(0.02)

            # If skipped, render final state instantly
            if skipped:
                renderer.lines.clear()
                renderer.phase1_header(elapsed())
                renderer.phase1_hw_checks(elapsed())
                renderer.phase2_header(elapsed())
                for agent in data.agents:
                    renderer.phase2_agent(agent, elapsed())
                renderer.add_blank()
                renderer.phase3_progress(1.0, elapsed())

            # Final status (always shown)
            renderer.phase3_status(elapsed())
            update()

            # Brief hold so the user can see it
            time.sleep(0.3)

    # After Live exits (transient=True clears it), print a compact summary
    # that stays in scrollback
    _print_static_summary(console, data)


# ---------------------------------------------------------------------------
# Static (non-animated) fallback
# ---------------------------------------------------------------------------


def _print_static_summary(console: Console, data: BootData) -> None:
    """Print a compact, non-animated summary that stays in scrollback."""
    ver = f" v{data.version}" if data.version else ""
    sep = f"[{C_DIM}]\u2500[/{C_DIM}]" * min(56, console.size.width - 2)

    console.print(sep)
    console.print(f"  [bold {C_GREEN}]BERNSTEIN[/bold {C_GREEN}][{C_DIM}]{ver}  orchestration system[/{C_DIM}]")

    if data.agents:
        parts: list[str] = []
        for a in data.agents:
            icon = f"[{C_GREEN}]ok[/{C_GREEN}]" if a.status == "ok" else f"[{C_DIM}]--[/{C_DIM}]"
            model_short = a.model.split("-")[-1] if a.model else "?"
            parts.append(f"{icon} {a.name}[{C_DIM}]/{model_short}[/{C_DIM}]")
        console.print("  " + "  ".join(parts))

    agent_ok = sum(1 for a in data.agents if a.status == "ok")
    task_label = f", {data.task_count} tasks" if data.task_count > 0 else ""
    console.print(f"  [{C_DIM}]{agent_ok} agents ready{task_label}[/{C_DIM}]")
    console.print(sep)


def _print_static(console: Console, data: BootData) -> None:  # pyright: ignore[reportUnusedFunction]
    """Non-animated fallback for non-TTY or skip_animation mode."""
    _print_static_summary(console, data)


# ---------------------------------------------------------------------------
# Data conversion helper
# ---------------------------------------------------------------------------


def _agents_from_dicts(agents: list[dict[str, Any]] | None) -> list[AgentProbe]:  # pyright: ignore[reportUnusedFunction]
    """Convert the legacy dict-based agent list to AgentProbe objects."""
    if not agents:
        return []

    probes: list[AgentProbe] = []
    for a in agents:
        name = str(a.get("name", "?"))
        logged_in = bool(a.get("logged_in", False))
        model = str(a.get("default_model", ""))

        if logged_in:
            status = "ok"
            detail = "authenticated"
        else:
            status = "warn"
            detail = "no API key"

        probes.append(AgentProbe(name=name, model=model, status=status, detail=detail))

    return probes


# ---------------------------------------------------------------------------
# Public API -- drop-in replacement for splash.splash()
# ---------------------------------------------------------------------------


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
    """Show the startup splash, falling back to compact on any error."""
    try:
        from pathlib import Path

        from bernstein.cli.splash_v2 import render_startup_splash
        from bernstein.core.visual_config import resolve_visual_config

        config = None
        if seed_file:
            try:
                from bernstein.core.seed import parse_seed

                seed_cfg = parse_seed(Path(seed_file))
                config = resolve_visual_config(getattr(seed_cfg, "visual", None))
            except Exception:
                config = resolve_visual_config(None)
        else:
            config = resolve_visual_config(None)

        render_startup_splash(
            console,
            version=version,
            agents=agents,
            seed_file=seed_file,
            goal_preview=goal_preview,
            budget=budget,
            task_count=task_count,
            skip_animation=skip_animation,
            config=config,
        )
    except Exception:
        # Premium splash failed — fall back to compact splash.
        from bernstein.cli.splash import splash as compact_splash

        compact_splash(
            console,
            version=version,
            agents=agents or [],
            seed_file=seed_file,
            goal_preview=goal_preview,
            budget=budget,
            task_count=task_count,
        )
