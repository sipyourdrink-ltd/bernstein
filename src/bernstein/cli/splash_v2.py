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

from bernstein.cli.gradients import BERNSTEIN_COLORS, linear_gradient
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
        skip_animation: bool = False,
        config: VisualConfig | None = None,
    ) -> None:
        self._console = console or Console()
        self._skip = skip_animation
        self._config = config or VisualConfig()

    def render(self, context: SplashContext | None = None) -> None:
        if not self._config.splash:
            return
        ctx = context or SplashContext()
        if not sys.stdout.isatty() or self._skip or os.environ.get("CI"):
            self._render_fallback(ctx)
            return
        self._render_premium(ctx)

    def _render_premium(self, ctx: SplashContext) -> None:
        w, h = shutil.get_terminal_size((80, 24))

        # 1. Build gradient background lines (plain ANSI, no cursor positioning).
        bg_lines = linear_gradient(w, h, BERNSTEIN_COLORS, direction="diagonal").splitlines()
        while len(bg_lines) < h:
            bg_lines.append(" " * w)

        # 2. Logo + layout.
        logo_lines = _load_logo()
        logo_colors = _logo_gradient(len(logo_lines))
        content_h = len(logo_lines) + 7
        logo_row = max(1, (h - content_h) // 2)

        # 3. Hide cursor, clear screen.
        sys.stdout.write("\033[?25l\033[2J\033[H")
        sys.stdout.flush()

        # 4. Scanline reveal: draw gradient from center outward.
        mid = h // 2
        reveal_order: list[int] = []
        for offset in range(mid + 1):
            if mid + offset < h:
                reveal_order.append(mid + offset)
            if offset > 0 and mid - offset >= 0:
                reveal_order.append(mid - offset)

        total_time = 0.8
        batch = max(1, len(reveal_order) // 20)
        step = total_time / max(len(reveal_order) / batch, 1)
        buf: list[str] = []

        for i, row in enumerate(reveal_order):
            buf.append(f"\033[{row + 1};1H{bg_lines[row]}")
            if (i + 1) % batch == 0 or i == len(reveal_order) - 1:
                sys.stdout.write("".join(buf))
                sys.stdout.flush()
                buf.clear()
                if not _key_pressed():
                    time.sleep(step)

        # 5. Overlay logo char-by-char (skip spaces → gradient shows through).
        out: list[str] = []
        for idx, logo_line in enumerate(logo_lines):
            row = logo_row + idx
            if row >= h:
                break
            pad = max(0, (w - len(logo_line)) // 2)
            color = logo_colors[idx] if idx < len(logo_colors) else "\033[1;97m"
            for col, ch in enumerate(logo_line):
                if ch != " ":
                    out.append(f"\033[{row + 1};{pad + col + 1}H{color}{ch}")

        # 6. Sub-pixel reflection (dim mirror of bottom logo lines).
        refl_start = logo_row + len(logo_lines)
        for idx in range(min(3, len(logo_lines))):
            src = logo_lines[len(logo_lines) - 1 - idx]
            row = refl_start + idx
            if row >= h:
                break
            pad = max(0, (w - len(src)) // 2)
            alpha = max(30, 70 - idx * 25)
            dim = f"\033[38;2;{alpha};{alpha + 20};{alpha + 40}m"
            for col, ch in enumerate(src):
                if ch != " ":
                    out.append(f"\033[{row + 1};{pad + col + 1}H{dim}{ch}")

        # 7. Subtitle.
        sub_row = refl_start + 4
        if sub_row < h:
            subtitle = "A G E N T   O R C H E S T R A"
            pad_s = max(0, (w - len(subtitle)) // 2)
            out.append(f"\033[{sub_row + 1};{pad_s + 1}H\033[1;38;2;0;212;255m{subtitle}")

        # 8. Probe lines.
        agent_names = ", ".join(
            str(a.get("name", "?")).title() for a in ctx.agents[:3]
        ) or "none detected"
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

        out.append("\033[0m")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

        # 9. Hold, then clear.
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
