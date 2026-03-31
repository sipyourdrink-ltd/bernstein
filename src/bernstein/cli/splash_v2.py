"""Tiered premium splash renderer for Bernstein."""

from __future__ import annotations

import os
import re
import select
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from rich.align import Align
from rich.console import Console

from bernstein.cli.crt_effects import CRTConfig, power_on_effect
from bernstein.cli.gradients import BERNSTEIN_COLORS, linear_gradient
from bernstein.cli.image_renderer import render_image
from bernstein.cli.splash import splash as compact_splash
from bernstein.cli.splash_assets import generate_progress_bar_image, generate_splash_image
from bernstein.cli.terminal_caps import TerminalCaps, detect_capabilities
from bernstein.cli.text_effects import logo_reveal, typing_effect
from bernstein.cli.visual_theme import PALETTE
from bernstein.core.visual_config import VisualConfig

# Custom block-art logo using Unicode half/quarter block characters.
# fmt: off
_LOGO_LINES: list[str] = [
    "    \u2584\u2584\u2584",
    "   \u2588\u2588\u2580\u2580\u2588\u2584                        \u2588\u2584",
    "   \u2588\u2588 \u2584\u2588\u2580       \u2584    \u2584          \u2584\u2588\u2588\u2584      \u2580\u2580 \u2584",  # noqa: E501
    "   \u2588\u2588\u2580\u2580\u2588\u2584 \u2584\u2588\u2580\u2588\u2584 \u2588\u2588\u2588\u2588\u2584\u2588\u2588\u2588\u2588\u2584 \u2584\u2588\u2588\u2580\u2588 \u2588\u2588 \u2584\u2588\u2580\u2588\u2584 \u2588\u2588 \u2588\u2588\u2588\u2588\u2584",  # noqa: E501
    " \u2584 \u2588\u2588  \u2584\u2588 \u2588\u2588\u2584\u2588\u2580 \u2588\u2588   \u2588\u2588 \u2588\u2588 \u2580\u2588\u2588\u2588\u2584 \u2588\u2588 \u2588\u2588\u2584\u2588\u2580 \u2588\u2588 \u2588\u2588 \u2588\u2588",  # noqa: E501
    " \u2580\u2588\u2588\u2588\u2588\u2588\u2588\u2580\u2584\u2580\u2588\u2584\u2584\u2584\u2588\u2580  \u2584\u2588\u2588 \u2580\u2588\u2588\u2584\u2584\u2588\u2588\u2580\u2584\u2588\u2588\u2584\u2580\u2588\u2584\u2584\u2584\u2588\u2588\u2584\u2588\u2588 \u2580\u2588",  # noqa: E501
]
# fmt: on


def _empty_agents() -> list[dict[str, object]]:
    """Return a correctly typed empty agent list for dataclass defaults."""
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
    """Tiered splash screen with automatic capability detection."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        skip_animation: bool = False,
        caps: TerminalCaps | None = None,
        config: VisualConfig | None = None,
    ) -> None:
        self._console = console or Console()
        self._caps = caps or detect_capabilities()
        self._skip = skip_animation
        self._config = config or VisualConfig()
        self._force_tier = self._config.splash_tier

    def render(self, context: SplashContext | None = None) -> None:
        """Render the best splash sequence for the current terminal."""
        if not self._config.splash:
            return

        ctx = context or SplashContext()
        tier = self._select_tier()
        if tier in ("tier1", "tier2") and self._config.crt_effects and not self._skip:
            power_on_effect(config=CRTConfig(width=self._caps.term_width, height=min(self._caps.term_height, 24)))

        if tier == "tier1":
            self._render_tier1(ctx)
        elif tier == "tier2":
            self._render_tier2(ctx)
        else:
            self._render_tier3(ctx)

    def _select_tier(self) -> str:
        """Resolve the active splash tier from config and capabilities."""
        if self._skip or not self._caps.is_tty or os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            return "tier3"
        if self._force_tier != "auto":
            return self._force_tier
        # Tier 1 only for Kitty/iTerm2 — they reliably render inline images.
        # Sixel is unreliable across terminals, so we prefer tier2 for it.
        if self._caps.kitty_graphics or self._caps.iterm2_inline:
            return "tier1"
        if self._caps.truecolor or self._caps.halfblocks:
            return "tier2"
        return "tier3"

    def _render_tier1(self, context: SplashContext) -> None:
        """Render the premium image-first splash."""
        width = max(34, min(self._caps.term_width - 6, 84))
        height = max(12, min(max(10, self._caps.term_height // 2), 20))
        image = generate_splash_image(
            width=width * 10,
            height=height * 20,
            version=context.version,
            agent_count=len(context.agents),
        )
        self._console.clear()
        render_image(image, width=width, height=height, caps=self._caps, synchronized=True)
        self._console.print()
        self._render_subtitle(animated=not self._skip)
        self._render_probe_sequence(context, animated=not self._skip, use_icons=True)
        progress = generate_progress_bar_image(width=min(480, width * 7), height=18, progress=1.0)
        render_image(progress, width=max(16, width // 2), height=2, caps=self._caps, synchronized=True)
        self._console.print()

    def _render_tier2(self, context: SplashContext) -> None:
        """Render the premium truecolor splash — full-screen gradient + centered logo."""
        w = self._caps.term_width
        h = self._caps.term_height

        # Hide cursor, clear screen.
        sys.stdout.write("\033[?25l\033[2J\033[H")
        sys.stdout.flush()

        # Full-screen gradient background (half-block chars → sub-cell resolution).
        bg = linear_gradient(w, h, BERNSTEIN_COLORS, direction="diagonal")
        bg_lines = bg.splitlines()

        # Custom block-art logo (Unicode half/quarter blocks for sub-cell detail).
        logo_lines = list(_LOGO_LINES)

        # Gradient colors for logo lines (ANSI escape codes, not Rich markup).
        logo_colors = _sample_ansi_gradient(len(logo_lines), BERNSTEIN_COLORS)

        # Vertical centering: logo + subtitle + 3 probe lines + spacing.
        content_height = len(logo_lines) + 6
        logo_start_row = max(1, (len(bg_lines) - content_height) // 2)

        # Render background, then overlay logo char-by-char (skip spaces
        # so the gradient shows through — transparent logo effect).
        buf = []
        for i, bg_line in enumerate(bg_lines):
            buf.append(f"\033[{i + 1};1H{bg_line}")
        # Overlay non-space logo characters individually.
        for logo_idx, logo_line in enumerate(logo_lines):
            row = logo_start_row + logo_idx
            if row >= len(bg_lines):
                break
            pad = max(0, (w - len(logo_line)) // 2)
            color = logo_colors[logo_idx] if logo_idx < len(logo_colors) else "\033[1;97m"
            for col_offset, ch in enumerate(logo_line):
                if ch != " ":
                    buf.append(f"\033[{row + 1};{pad + col_offset + 1}H{color}{ch}")
        buf.append("\033[0m")

        # Subtitle — bold cyan, centered.
        subtitle_row = logo_start_row + len(logo_lines) + 1
        subtitle = "A G E N T   O R C H E S T R A"
        pad_s = max(0, (w - len(subtitle)) // 2)
        buf.append(f"\033[{subtitle_row};{pad_s + 1}H\033[1;38;2;0;212;255m{subtitle}\033[0m")

        # Probe lines — instant, centered, dim cyan.
        probes = self._probe_lines(context, use_icons=False)
        for j, probe in enumerate(probes):
            clean = re.sub(r"\[.*?\]", "", probe)
            pad_p = max(0, (w - len(clean)) // 2)
            buf.append(f"\033[{subtitle_row + 2 + j};{pad_p + 1}H\033[38;2;100;180;200m{clean}\033[0m")

        # Single atomic write for flicker-free rendering.
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

        # Hold splash for 3.5 seconds (user admires; background imports happen).
        if not self._skip:
            time.sleep(3.5)
        # Clear screen and restore cursor.
        sys.stdout.write("\033[0m\033[2J\033[H\033[?25h")
        sys.stdout.flush()

    def _render_tier3(self, context: SplashContext) -> None:
        """Render the minimal fallback splash."""
        compact_splash(
            self._console,
            version=context.version,
            agents=context.agents,
            seed_file=context.seed_file,
            goal_preview=context.goal_preview,
            budget=context.budget,
            task_count=context.task_count,
            skip_animation=True,
        )

    def _render_subtitle(self, *, animated: bool) -> None:
        """Render the subtitle beneath the hero element."""
        subtitle = "AGENT ORCHESTRA"
        if animated and self._caps.is_tty:
            logo_reveal(subtitle, effect="decrypt", colors=[PALETTE.glow, PALETTE.cyan])
        else:
            self._console.print(Align.center(f"[bold {PALETTE.glow}]{subtitle}[/]"))

    def _render_probe_sequence(self, context: SplashContext, *, animated: bool, use_icons: bool) -> None:
        """Render the boot probe sequence."""
        lines = self._probe_lines(context, use_icons=use_icons)
        if animated and self._caps.is_tty:
            for line in lines:
                typing_effect([line], speed=0.012)
                if _key_pressed():
                    break
                time.sleep(0.08)
        else:
            for line in lines:
                self._console.print(line)

    def _probe_lines(self, context: SplashContext, *, use_icons: bool) -> list[str]:
        """Return human-readable probe lines for the splash."""
        check = "✓" if use_icons else "[bold green]✓[/]"
        caps = self._describe_caps()
        agent_names = ", ".join(str(agent.get("name", "?")).title() for agent in context.agents[:3]) or "none detected"
        return [
            f"{check} Terminal: {caps}",
            f"{check} Agents: {agent_names}",
            f"{check} Task server: {context.task_server_url}",
        ]

    def _describe_caps(self) -> str:
        """Describe the detected terminal capabilities."""
        features: list[str] = []
        if self._caps.truecolor:
            features.append("truecolor")
        if self._caps.kitty_graphics:
            features.append("kitty")
        elif self._caps.iterm2_inline:
            features.append("iterm2-inline")
        elif self._caps.sixel:
            features.append("sixel")
        if self._caps.halfblocks:
            features.append("halfblocks")
        return ", ".join(features) if features else "basic tty"

    def _animate_progress_bar(self, *, animated: bool) -> None:
        """Render the final startup progress bar."""
        total = 20
        colors = ["#0D5E73", "#17A1B8", PALETTE.glow]
        if not animated:
            self._console.print(self._format_progress(total, total, colors))
            return
        for index in range(total + 1):
            sys.stdout.write("\r" + self._format_progress(index, total, colors, markup=False))
            sys.stdout.flush()
            if _key_pressed():
                break
            time.sleep(0.03)
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _format_progress(
        self,
        current: int,
        total: int,
        colors: list[str],
        *,
        markup: bool = True,
    ) -> str:
        """Format a gradient text progress bar."""
        filled = max(0, min(total, current))
        if total <= 0:
            return "Ready"
        segment_colors = _progress_colors(total, colors)
        parts: list[str] = []
        for idx in range(total):
            if idx < filled:
                color = segment_colors[idx]
                token = f"[bold {color}]█[/]" if markup else _ansi_block(color)
                parts.append(token)
            else:
                parts.append("[dim]░[/]" if markup else "░")
        suffix = f" {int(filled / total * 100)}%"
        return "Boot: " + "".join(parts) + suffix


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
    """Compatibility entrypoint shared by ``main.py`` and ``splash_screen.py``."""
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


def _render_figlet_raw(text: str, max_width: int = 80) -> str:
    """Render FIGlet text as plain ASCII (no Rich markup, no color)."""
    try:
        import pyfiglet
    except ImportError:
        return text
    for font in ("slant", "small", "standard", "mini"):
        try:
            figlet = pyfiglet.Figlet(font=font)
            rendered = str(figlet.renderText(text))
            lines = [line.rstrip() for line in rendered.splitlines()]
            if all(len(line) <= max_width for line in lines if line):
                return "\n".join(lines)
        except Exception:
            continue
    return text


def _sample_ansi_gradient(count: int, colors: object) -> list[str]:
    """Return ANSI bold+foreground escape codes sampled across a color gradient.

    Accepts colors as RGB tuples ``(r, g, b)`` or hex strings ``#RRGGBB``.
    """
    color_list = list(colors) if not isinstance(colors, list) else colors
    if count <= 0 or not color_list:
        return []

    # Normalise each color to (r, g, b) int tuple.
    rgb: list[tuple[int, int, int]] = []
    for c in color_list:
        if isinstance(c, (list, tuple)):
            rgb.append((int(c[0]), int(c[1]), int(c[2])))
        elif isinstance(c, str):
            h = c.lstrip("#")
            rgb.append((int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))
        else:
            rgb.append((255, 255, 255))

    # Use a brighter gradient for the logo: white → cyan → teal.
    logo_gradient: list[tuple[int, int, int]] = [
        (220, 240, 255),  # near-white
        (100, 220, 255),  # bright cyan
        (0, 180, 220),    # teal
        (50, 210, 255),   # cyan
        (200, 235, 255),  # near-white again
    ]

    results: list[str] = []
    for i in range(count):
        t = i / max(1, count - 1)
        idx = t * (len(logo_gradient) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(logo_gradient) - 1)
        frac = idx - lo
        r1, g1, b1 = logo_gradient[lo]
        r2, g2, b2 = logo_gradient[hi]
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


def _progress_colors(total: int, colors: list[str]) -> list[str]:
    """Sample a color list for progress-bar segments."""
    from bernstein.cli.visual_theme import sample_gradient

    return list(sample_gradient(tuple(colors), total))


def _ansi_block(color: str) -> str:
    """Render a colored block for the ANSI progress bar path."""
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    return f"\033[38;2;{red};{green};{blue}m█\033[0m"
