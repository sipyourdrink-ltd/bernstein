"""Tests for live splash wiring and compatibility wrappers."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import bernstein.cli.advanced_cmd as advanced_cmd
import pytest
from bernstein.cli.terminal_caps import TerminalCaps
from bernstein.core.visual_config import VisualConfig
from rich.console import Console


def _live_callback() -> Callable[[float, bool, bool], None]:
    callback = advanced_cmd.live.callback
    assert callback is not None
    return cast("Callable[[float, bool, bool], None]", callback)


def test_live_command_skips_splash_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeDashboardApp:
        _play_power_off_on_exit = False
        _restart_on_exit = False

        def run(self) -> None:
            calls.append("run")

    def fake_load(seed_path: object) -> None:
        return None

    def fake_visual(seed_cfg: object, no_splash: bool) -> VisualConfig:
        return VisualConfig(splash=False)

    def fake_splash(*args: object, **kwargs: object) -> None:
        calls.append("splash")

    monkeypatch.setattr(advanced_cmd, "find_seed_file", lambda: None)
    monkeypatch.setattr(advanced_cmd, "_load_live_seed_config", fake_load)
    monkeypatch.setattr(advanced_cmd, "_resolve_live_visual_config", fake_visual)
    monkeypatch.setattr("bernstein.cli.dashboard.BernsteinApp", FakeDashboardApp)
    monkeypatch.setattr("bernstein.cli.splash_v2.render_startup_splash", fake_splash)

    _live_callback()(2.0, False, True)

    assert calls == ["run"]


def test_live_command_runs_power_off_after_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeDashboardApp:
        _play_power_off_on_exit = True
        _restart_on_exit = False

        def run(self) -> None:
            calls.append("run")

    def fake_load(seed_path: object) -> None:
        return None

    def fake_visual(seed_cfg: object, no_splash: bool) -> VisualConfig:
        return VisualConfig(splash=False, crt_effects=True)

    def fake_poweroff(**kwargs: object) -> None:
        calls.append("poweroff")

    def fake_caps() -> TerminalCaps:
        return TerminalCaps(
            is_tty=True,
            supports_truecolor=True,
            supports_256color=True,
            supports_kitty=False,
            supports_iterm2=False,
            supports_sixel=False,
            term_width=100,
            term_height=30,
        )

    monkeypatch.setattr(advanced_cmd, "find_seed_file", lambda: None)
    monkeypatch.setattr(advanced_cmd, "_load_live_seed_config", fake_load)
    monkeypatch.setattr(advanced_cmd, "_resolve_live_visual_config", fake_visual)
    monkeypatch.setattr("bernstein.cli.dashboard.BernsteinApp", FakeDashboardApp)
    monkeypatch.setattr("bernstein.cli.crt_effects.power_off_effect", fake_poweroff)
    monkeypatch.setattr("bernstein.cli.terminal_caps.detect_capabilities", fake_caps)

    _live_callback()(2.0, False, False)

    assert calls == ["run", "poweroff"]


def test_splash_screen_wrapper_delegates_to_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli import splash_screen

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_render(*args: object, **kwargs: object) -> None:
        calls.append((args, dict(kwargs)))

    def fake_resolve(raw: object | None = None) -> VisualConfig:
        return VisualConfig()

    monkeypatch.setattr("bernstein.cli.splash_v2.render_startup_splash", fake_render)
    monkeypatch.setattr("bernstein.core.visual_config.resolve_visual_config", fake_resolve)

    splash_screen.splash(Console(record=True), version="1.2.3", agents=[{"name": "codex"}], task_count=2)

    assert calls
    assert calls[0][1]["version"] == "1.2.3"
    assert calls[0][1]["task_count"] == 2
