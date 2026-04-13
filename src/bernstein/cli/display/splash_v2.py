"""Unified premium splash renderer for Bernstein.

Single rendering path: full-screen gradient, scanline reveal,
block-art logo overlay with transparency, sub-pixel reflection.
"""

from __future__ import annotations

import os
import select
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from bernstein.cli.crt_effects import power_on_effect
from bernstein.cli.gradients import BERNSTEIN_COLORS, linear_gradient
from bernstein.cli.terminal_caps import TerminalCaps, detect_capabilities
from bernstein.core.visual_config import VisualConfig


def _load_logo() -> list[str]:
    """Load logo from docs/assets/ascii_logo.md, stripping empty lines."""
    asset = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "assets" / "ascii_logo.md"
    if not asset.exists():
        return ["  BERNSTEIN"]
    return [line for line in asset.read_text(encoding="utf-8").splitlines() if line.strip()]


def _empty_agents() -> list[dict[str, object]]:
    return []


@dataclass(frozen=True)
class SplashContext:
    """Data needed to render the startup splash."""

    version: str = ""
    agents: list[dict[str, object]] = field(default_factory=_empty_agents)
    seed_file: str | None = None
    goal_preview: str = ""
    budget: float = 0.0
    task_count: int = 0
    task_server_url: str = "http://127.0.0.1:8052"


class SplashRenderer:
    """Unified splash: scanline reveal + gradient + logo overlay."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        caps: TerminalCaps | None = None,
        skip_animation: bool = False,
        config: VisualConfig | None = None,
    ) -> None:
        self._console = console or Console()
        self._caps = caps or detect_capabilities()
        self._skip = skip_animation
        self._config = config or VisualConfig()

    def render(self, context: SplashContext | None = None) -> None:
        if not self._config.splash:
            return
        ctx = context or SplashContext()
        tier = self._select_tier()
        if self._skip or os.environ.get("CI"):
            tier = "tier3"
        if tier == "tier1":
            self._render_tier1(ctx)
        elif tier == "tier2":
            self._render_tier2(ctx)
        else:
            self._render_tier3(ctx)

    def _select_tier(self) -> str:
        if self._config.splash_tier != "auto":
            return self._config.splash_tier
        if not self._caps.is_tty:
            return "tier3"
        if self._caps.supports_kitty or self._caps.supports_iterm2 or self._caps.supports_sixel:
            return "tier1"
        if self._caps.supports_truecolor or self._caps.supports_256color:
            return "tier2"
        return "tier3"

    def _render_tier1(self, ctx: SplashContext) -> None:
        if self._config.crt_effects and not self._skip:
            power_on_effect()
        self._render_premium(ctx)

    def _render_tier2(self, ctx: SplashContext) -> None:
        self._render_premium(ctx)

    def _render_tier3(self, ctx: SplashContext) -> None:
        self._render_fallback(ctx)

    @staticmethod
    def _diamond_reveal(bg_lines: list[str], h: int) -> None:
        """Animate a diamond-shaped reveal of background gradient lines."""
        mid = h // 2
        max_dist = mid + 1
        wave_groups: list[list[int]] = [[] for _ in range(max_dist + 1)]
        for row in range(h):
            dist = abs(row - mid)
            wave_groups[min(dist, max_dist)].append(row)

        non_empty = [g for g in wave_groups if g]
        step = 0.8 / max(len(non_empty), 1)

        for group in non_empty:
            buf_part = "".join(f"\033[{r + 1};1H{bg_lines[r]}" for r in group)
            sys.stdout.write(buf_part)
            sys.stdout.flush()
            if not _key_pressed():
                time.sleep(step)

    @staticmethod
    def _render_line_chars(line: str, row: int, pad: int, color: str) -> list[str]:
        """Render non-space characters of a line with ANSI positioning."""
        return [f"\033[{row + 1};{pad + col + 1}H{color}{ch}" for col, ch in enumerate(line) if ch != " "]

    @staticmethod
    def _overlay_logo(logo_lines: list[str], logo_colors: list[str], logo_row: int, w: int, h: int) -> list[str]:
        """Build ANSI escape sequences for the logo and its reflection."""
        out: list[str] = []
        for idx, logo_line in enumerate(logo_lines):
            row = logo_row + idx
            if row >= h:
                break
            pad = max(0, (w - len(logo_line)) // 2)
            color = logo_colors[idx] if idx < len(logo_colors) else "\033[1;97m"
            out.extend(SplashRenderer._render_line_chars(logo_line, row, pad, color))

        # Sub-pixel reflection (dim mirror of bottom logo lines)
        refl_start = logo_row + len(logo_lines)
        for idx in range(min(3, len(logo_lines))):
            src = logo_lines[len(logo_lines) - 1 - idx]
            row = refl_start + idx
            if row >= h:
                break
            pad = max(0, (w - len(src)) // 2)
            alpha = max(30, 70 - idx * 25)
            dim = f"\033[38;2;{alpha};{alpha + 20};{alpha + 40}m"
            out.extend(SplashRenderer._render_line_chars(src, row, pad, dim))

        return out

    @staticmethod
    def _overlay_text(ctx: SplashContext, sub_row: int, w: int, h: int) -> list[str]:
        """Build ANSI escape sequences for the subtitle and probe lines."""
        out: list[str] = []
        if sub_row < h:
            subtitle = "A G E N T   O R C H E S T R A"
            pad_s = max(0, (w - len(subtitle)) // 2)
            out.append(f"\033[{sub_row + 1};{pad_s + 1}H\033[1;38;2;0;212;255m{subtitle}")

        agent_names = ", ".join(str(a.get("name", "?")).title() for a in ctx.agents[:3]) or "none detected"
        probes = [
            f"\u2713 Terminal: truecolor, {w}x{h}",
            f"\u2713 Agents: {agent_names}",
            f"\u2713 Server: {ctx.task_server_url}",
        ]
        for j, probe in enumerate(probes):
            row = sub_row + 2 + j
            if row >= h:
                break
            pad_p = max(0, (w - len(probe)) // 2)
            out.append(f"\033[{row + 1};{pad_p + 1}H\033[38;2;100;180;200m{probe}")
        return out

    def _render_premium(self, ctx: SplashContext) -> None:
        w, h = shutil.get_terminal_size((80, 24))

        bg_lines = linear_gradient(w, h, BERNSTEIN_COLORS, direction="diagonal").splitlines()
        while len(bg_lines) < h:
            bg_lines.append(" " * w)

        logo_lines = _load_logo()
        logo_colors = _logo_gradient(len(logo_lines))
        content_h = len(logo_lines) + 7
        logo_row = max(1, (h - content_h) // 2)

        sys.stdout.write("\033[?25l\033[2J\033[H")
        sys.stdout.flush()

        self._diamond_reveal(bg_lines, h)

        out = self._overlay_logo(logo_lines, logo_colors, logo_row, w, h)

        refl_start = logo_row + len(logo_lines)
        sub_row = refl_start + 4
        out.extend(self._overlay_text(ctx, sub_row, w, h))

        out.append("\033[0m")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

        if not self._skip:
            time.sleep(2.5)
        sys.stdout.write("\033[0m\033[2J\033[H\033[?25h")
        sys.stdout.flush()

    def _render_fallback(self, ctx: SplashContext) -> None:
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


def _logo_gradient(count: int) -> list[str]:
    """ANSI bold+fg codes for logo lines: white -> cyan -> teal."""
    grad = [
        (220, 240, 255),
        (100, 220, 255),
        (0, 180, 220),
        (50, 210, 255),
        (200, 235, 255),
    ]
    results: list[str] = []
    for i in range(count):
        t = i / max(1, count - 1)
        idx = t * (len(grad) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(grad) - 1)
        frac = idx - lo
        r1, g1, b1 = grad[lo]
        r2, g2, b2 = grad[hi]
        r = int(r1 * (1 - frac) + r2 * frac)
        g = int(g1 * (1 - frac) + g2 * frac)
        b = int(b1 * (1 - frac) + b2 * frac)
        results.append(f"\033[1;38;2;{r};{g};{b}m")
    return results


def _key_pressed() -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return bool(select.select([sys.stdin], [], [], 0.0)[0])
    except (OSError, ValueError):
        return False


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
            agents=[dict(a) for a in (agents or [])],
            seed_file=seed_file,
            goal_preview=goal_preview,
            budget=budget,
            task_count=task_count,
        )
    )
