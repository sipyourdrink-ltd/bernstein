"""Tests for bernstein.cli.splash_v2."""

from __future__ import annotations

from unittest.mock import patch

from bernstein.cli.splash_v2 import SplashContext, SplashRenderer, render_startup_splash
from bernstein.cli.terminal_caps import TerminalCaps
from bernstein.core.visual_config import VisualConfig
from rich.console import Console


def _caps(
    *,
    is_tty: bool = True,
    truecolor: bool = True,
    kitty: bool = False,
    iterm2: bool = False,
    sixel: bool = False,
) -> TerminalCaps:
    return TerminalCaps(
        is_tty=is_tty,
        supports_truecolor=truecolor,
        supports_256color=truecolor,
        supports_kitty=kitty,
        supports_iterm2=iterm2,
        supports_sixel=sixel,
        term_width=120,
        term_height=40,
    )


@patch.dict("os.environ", {}, clear=True)
def test_select_tier_prefers_tier1_for_image_terminals() -> None:
    renderer = SplashRenderer(Console(record=True), caps=_caps(kitty=True), config=VisualConfig(splash_tier="auto"))

    assert renderer._select_tier() == "tier1"


@patch.dict("os.environ", {}, clear=True)
def test_select_tier_prefers_tier2_for_truecolor_terminal() -> None:
    renderer = SplashRenderer(Console(record=True), caps=_caps(truecolor=True), config=VisualConfig(splash_tier="auto"))

    assert renderer._select_tier() == "tier2"


def test_select_tier_returns_tier3_for_non_tty() -> None:
    renderer = SplashRenderer(Console(record=True), caps=_caps(is_tty=False), config=VisualConfig(splash_tier="auto"))

    assert renderer._select_tier() == "tier3"


@patch.dict("os.environ", {}, clear=True)
def test_render_dispatches_to_selected_tier(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    renderer = SplashRenderer(Console(record=True), caps=_caps(kitty=True), config=VisualConfig(splash_tier="auto"))
    monkeypatch.setattr(renderer, "_render_tier1", lambda context: calls.append("tier1"))
    monkeypatch.setattr(renderer, "_render_tier2", lambda context: calls.append("tier2"))
    monkeypatch.setattr(renderer, "_render_tier3", lambda context: calls.append("tier3"))
    monkeypatch.setattr("bernstein.cli.splash_v2.power_on_effect", lambda **kwargs: None)

    renderer.render(SplashContext(version="1.0"))

    assert calls == ["tier1"]


def test_render_startup_splash_builds_context(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[SplashContext] = []

    def fake_render(self: SplashRenderer, context: SplashContext | None = None) -> None:
        if context is not None:
            seen.append(context)

    monkeypatch.setattr(SplashRenderer, "render", fake_render)

    render_startup_splash(
        Console(record=True),
        version="1.2.3",
        agents=[{"name": "codex"}],
        seed_file="bernstein.yaml",
        goal_preview="Ship it",
        budget=4.2,
        task_count=3,
    )

    assert seen
    assert seen[0].version == "1.2.3"
    assert seen[0].task_count == 3


def test_render_respects_disabled_splash(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    renderer = SplashRenderer(Console(record=True), caps=_caps(kitty=True), config=VisualConfig(splash=False))
    calls: list[str] = []
    monkeypatch.setattr(renderer, "_render_tier1", lambda context: calls.append("tier1"))

    renderer.render(SplashContext())

    assert calls == []
