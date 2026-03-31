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
from bernstein.cli.figlet_logo import render_logo
from bernstein.cli.gradients import BERNSTEIN_COLORS, linear_gradient
from bernstein.cli.image_renderer import render_image
from bernstein.cli.splash import splash as compact_splash
from bernstein.cli.splash_assets import generate_progress_bar_image, generate_splash_image
from bernstein.cli.terminal_caps import TerminalCaps, detect_capabilities
from bernstein.cli.text_effects import logo_reveal, typing_effect
from bernstein.cli.visual_theme import PALETTE
from bernstein.core.visual_config import VisualConfig


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
        self._console.clear()

        # Full-screen gradient background rendered as half-block chars.
        # This gives sub-cell vertical resolution (2 pixels per row).
        bg = linear_gradient(w, h, BERNSTEIN_COLORS, direction="diagonal")

        # Render the FIGlet logo.
        logo_text = render_logo(max_width=max(32, w - 4))
        logo_lines = logo_text.rstrip().splitlines()

        # Overlay logo onto the gradient — center vertically and horizontally.
        bg_lines = bg.splitlines()
        logo_start_row = max(1, (len(bg_lines) - len(logo_lines) - 6) // 2)

        # Print background with logo overlaid using cursor positioning.
        sys.stdout.write("\033[H")  # cursor home
        for i, bg_line in enumerate(bg_lines):
            if logo_start_row <= i < logo_start_row + len(logo_lines):
                # Overlay logo line (centered) on this row.
                logo_line = logo_lines[i - logo_start_row]
                # Strip ANSI from logo to measure width, then center.
                clean_logo = re.sub(r"\033\[[^m]*m", "", logo_line)
                pad = max(0, (w - len(clean_logo)) // 2)
                # Print: background start + gap + bold white logo + reset
                sys.stdout.write(f"\033[{i + 1};1H\033[1;97m{' ' * pad}{logo_line}\033[0m")
            else:
                sys.stdout.write(f"\033[{i + 1};1H{bg_line}")
        sys.stdout.flush()

        # Subtitle and probes below the logo — fast, no animation.
        subtitle_row = logo_start_row + len(logo_lines) + 1
        subtitle = "AGENT ORCHESTRA"
        pad = max(0, (w - len(subtitle)) // 2)
        sys.stdout.write(f"\033[{subtitle_row};1H\033[1;38;2;0;255;65m{' ' * pad}{subtitle}\033[0m\n")

        # Probe lines — instant, no typing effect.
        lines = self._probe_lines(context, use_icons=False)
        for j, line in enumerate(lines):
            clean = re.sub(r"\[.*?\]", "", line)
            pad_l = max(0, (w - len(clean)) // 2)
            sys.stdout.write(f"\033[{subtitle_row + 2 + j};1H{' ' * pad_l}{clean}")
        sys.stdout.flush()

        # Brief pause then continue.
        if not self._skip:
            time.sleep(1.5)
        sys.stdout.write("\033[0m")
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
