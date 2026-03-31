"""Unified premium splash renderer for Bernstein.

Single rendering path for all TTY terminals:
- Full-screen diagonal gradient (half-block sub-pixel resolution)
- Scanline reveal animation (lines unfurl from center)
- Block-art logo overlay with gradient coloring
- Sub-pixel reflection/glow effect under logo
- Probe lines with system info
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from bernstein.cli.gradients import BERNSTEIN_COLORS, linear_gradient
from bernstein.core.visual_config import VisualConfig


def _load_logo() -> list[str]:
    """Load logo from docs/assets/ascii_logo.md, stripping empty lines."""
    asset = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "assets" / "ascii_logo.md"
    if not asset.exists():
        return ["  BERNSTEIN"]
    lines = asset.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip()]


def _empty_agents() -> list[dict[str, object]]:
    return []


@dataclass(frozen=True)
class SplashContext:
    """Data needed to render the Bernstein startup splash."""

    version: str = ""
    agents: list[dict[str, object]] = field(default_factory=_empty_agents)
    seed_file: str | None = None
    goal_preview: str = ""
    budget: float = 0.0
    task_count: int = 0
    task_server_url: str = "http://127.0.0.1:8052"


class SplashRenderer:
    """Unified splash with scanline reveal, gradient background, and logo overlay."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        skip_animation: bool = False,
        config: VisualConfig | None = None,
    ) -> None:
        self._console = console or Console()
        self._skip = skip_animation
        self._config = config or VisualConfig()

    def render(self, context: SplashContext | None = None) -> None:
        """Render the splash sequence."""
        if not self._config.splash:
            return
        ctx = context or SplashContext()

        if not sys.stdout.isatty() or self._skip or os.environ.get("CI"):
            self._render_fallback(ctx)
            return

        self._render_premium(ctx)

    def _render_premium(self, ctx: SplashContext) -> None:
        """Single unified premium splash for all TTY terminals."""
        import shutil

        w, h = shutil.get_terminal_size((80, 24))

        # Build all frame data upfront.
        bg = linear_gradient(w, h, BERNSTEIN_COLORS, direction="diagonal")
        bg_lines = bg.splitlines()

        logo_lines = _load_logo()
        logo_colors = _logo_gradient(len(logo_lines))

        # Layout: center logo vertically with room for subtitle + probes.
        content_h = len(logo_lines) + 7
        logo_row = max(1, (h - content_h) // 2)

        # Build the complete final frame.
        frame = _build_frame(
            bg_lines, logo_lines, logo_colors, logo_row, w, h,
            ctx, self._describe_caps(),
        )

        # Hide cursor.
        sys.stdout.write("\033[?25l\033[2J\033[H")
        sys.stdout.flush()

        # === Scanline reveal: unfurl from center outward ===
        mid = h // 2
        reveal_order = []
        for offset in range(mid + 1):
            if mid + offset < h:
                reveal_order.append(mid + offset)
            if offset > 0 and mid - offset >= 0:
                reveal_order.append(mid - offset)

        # Render in 0.8 seconds total.
        step_delay = 0.8 / max(len(reveal_order), 1)
        buf: list[str] = []
        batch_size = max(1, len(reveal_order) // 20)  # 20 visual steps

        for i, row in enumerate(reveal_order):
            if row < len(frame):
                buf.append(f"\033[{row + 1};1H{frame[row]}")
            if (i + 1) % batch_size == 0 or i == len(reveal_order) - 1:
                sys.stdout.write("".join(buf))
                sys.stdout.flush()
                buf.clear()
                if not _key_pressed():
                    time.sleep(step_delay * batch_size)

        # Hold the complete frame.
        if not self._skip:
            time.sleep(2.5)

        # Clean exit — clear screen.
        sys.stdout.write("\033[0m\033[2J\033[H\033[?25h")
        sys.stdout.flush()

    def _render_fallback(self, ctx: SplashContext) -> None:
        """Minimal fallback for CI / pipe / non-TTY."""
        from bernstein.cli.splash import splash as compact_splash

        compact_splash(
            self._console,
            version=ctx.version,
            agents=ctx.agents,
            seed_file=ctx.seed_file,
            goal_preview=ctx.goal_preview,
            budget=ctx.budget,
            task_count=ctx.task_count,
            skip_animation=True,
        )

    def _describe_caps(self) -> str:
        """Describe terminal capabilities for probe line."""
        import shutil

        w, h = shutil.get_terminal_size((80, 24))
        return f"truecolor, {w}x{h}"


def _build_frame(
    bg_lines: list[str],
    logo_lines: list[str],
    logo_colors: list[str],
    logo_row: int,
    w: int,
    h: int,
    ctx: SplashContext,
    caps_desc: str,
) -> list[str]:
    """Compose the complete splash frame: gradient + logo + reflection + text."""
    frame = list(bg_lines)

    # Ensure frame has exactly h lines.
    while len(frame) < h:
        frame.append("")

    # Overlay logo characters (skip spaces for transparency).
    for idx, logo_line in enumerate(logo_lines):
        row = logo_row + idx
        if row >= h:
            break
        pad = max(0, (w - len(logo_line)) // 2)
        color = logo_colors[idx] if idx < len(logo_colors) else "\033[1;97m"
        overlay = _overlay_chars(frame[row], logo_line, pad, color)
        frame[row] = overlay

    # Sub-pixel reflection: dim, vertically flipped logo below.
    reflection_row = logo_row + len(logo_lines)
    for idx in range(min(3, len(logo_lines))):
        src_idx = len(logo_lines) - 1 - idx
        row = reflection_row + idx
        if row >= h:
            break
        logo_line = logo_lines[src_idx]
        pad = max(0, (w - len(logo_line)) // 2)
        # Dim reflection with fade.
        alpha = max(40, 80 - idx * 25)
        dim_color = f"\033[38;2;{alpha};{alpha + 30};{alpha + 50}m"
        overlay = _overlay_chars(frame[row], logo_line, pad, dim_color)
        frame[row] = overlay

    # Subtitle.
    subtitle_row = reflection_row + 4
    if subtitle_row < h:
        subtitle = "A G E N T   O R C H E S T R A"
        pad_s = max(0, (w - len(subtitle)) // 2)
        frame[subtitle_row] = (
            frame[subtitle_row][:0]
            + f"\033[{subtitle_row + 1};{pad_s + 1}H"
            + f"\033[1;38;2;0;212;255m{subtitle}\033[0m"
        )

    # Probe lines.
    agent_names = ", ".join(
        str(a.get("name", "?")).title() for a in ctx.agents[:3]
    ) or "detecting..."
    probes = [
        f"\u2713 Terminal: {caps_desc}",
        f"\u2713 Agents: {agent_names}",
        f"\u2713 Server: {ctx.task_server_url}",
    ]
    for j, probe in enumerate(probes):
        row = subtitle_row + 2 + j
        if row >= h:
            break
        pad_p = max(0, (w - len(probe)) // 2)
        frame[row] = (
            f"\033[{row + 1};{pad_p + 1}H"
            + f"\033[38;2;100;180;200m{probe}\033[0m"
        )

    # 3D depth: vignette — darken top and bottom 3 rows.
    for i in range(min(3, h)):
        dark = 30 + i * 15  # 30, 45, 60 brightness
        vignette = f"\033[38;2;{dark};{dark};{dark}m"
        # Top vignette (decorative sub-pixel line).
        bar = "".join("▁" if (c + i) % 3 == 0 else " " for c in range(w))
        frame[i] = f"\033[{i + 1};1H{vignette}{bar}\033[0m"
        # Bottom vignette.
        bi = h - 1 - i
        bar_b = "".join("▔" if (c + i) % 3 == 0 else " " for c in range(w))
        frame[bi] = f"\033[{bi + 1};1H{vignette}{bar_b}\033[0m"

    return frame


def _overlay_chars(
    bg_line: str,
    text: str,
    offset: int,
    color: str,
) -> str:
    """Overlay non-space characters from text onto bg_line at offset."""
    parts: list[str] = []
    row_match = re.match(r"\033\[(\d+);\d+H", bg_line)
    row_num = int(row_match.group(1)) if row_match else 1

    # Start with the background line.
    parts.append(f"\033[{row_num};1H{bg_line}")

    # Overlay each non-space character.
    for col, ch in enumerate(text):
        if ch != " ":
            parts.append(f"\033[{row_num};{offset + col + 1}H{color}{ch}")
    parts.append("\033[0m")
    return "".join(parts)


def _logo_gradient(count: int) -> list[str]:
    """Return ANSI bold+fg codes for logo lines: white → cyan → teal."""
    gradient = [
        (220, 240, 255),
        (100, 220, 255),
        (0, 180, 220),
        (50, 210, 255),
        (200, 235, 255),
    ]
    results: list[str] = []
    for i in range(count):
        t = i / max(1, count - 1)
        idx = t * (len(gradient) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(gradient) - 1)
        frac = idx - lo
        r1, g1, b1 = gradient[lo]
        r2, g2, b2 = gradient[hi]
        r = int(r1 * (1 - frac) + r2 * frac)
        g = int(g1 * (1 - frac) + g2 * frac)
        b = int(b1 * (1 - frac) + b2 * frac)
        results.append(f"\033[1;38;2;{r};{g};{b}m")
    return results


def _key_pressed() -> bool:
    """Return True when stdin has a waiting keypress."""
    if not sys.stdin.isatty():
        return False
    try:
        return bool(select.select([sys.stdin], [], [], 0.0)[0])
    except (OSError, ValueError):
        return False


# Keep compatibility entrypoint.
def render_startup_splash(
    console: Console,
    *,
    version: str = "",
    agents: list[dict[str, Any]] | None = None,
    seed_file: str | None = None,
    goal_preview: str = "",
    budget: float = 0.0,
    task_count: int = 0,
    skip_animation: bool = False,
    config: VisualConfig | None = None,
) -> None:
    """Compatibility entrypoint for main.py and splash_screen.py."""
    renderer = SplashRenderer(console, skip_animation=skip_animation, config=config)
    renderer.render(
        SplashContext(
            version=version,
            agents=[dict(agent) for agent in (agents or [])],
            seed_file=seed_file,
            goal_preview=goal_preview,
            budget=budget,
            task_count=task_count,
        )
    )
