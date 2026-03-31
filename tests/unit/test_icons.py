"""Tests for Nerd Font icon integration (GFX-09)."""

from __future__ import annotations

import os
from unittest.mock import patch

from bernstein.cli.icons import (
    NerdFontIcons,
    UnicodeFallbackIcons,
    get_icons,
)

# -- Detection ---------------------------------------------------------------


def test_get_icons_returns_nerd_font_when_env_var_set() -> None:
    """Returns NerdFontIcons when NERD_FONT=1 is set."""
    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icons = get_icons()
    assert isinstance(icons, NerdFontIcons)


def test_get_icons_returns_nerd_font_when_bernstein_env_var_set() -> None:
    """Returns NerdFontIcons when BERNSTEIN_NERD_FONT=1 is set."""
    with patch.dict(os.environ, {"BERNSTEIN_NERD_FONT": "1"}, clear=False):
        icons = get_icons()
    assert isinstance(icons, NerdFontIcons)


def test_get_icons_returns_unicode_fallback_by_default() -> None:
    """Returns UnicodeFallbackIcons when no Nerd Font env var is set."""
    env = {k: v for k, v in os.environ.items() if k not in ("NERD_FONT", "BERNSTEIN_NERD_FONT")}
    with patch.dict(os.environ, env, clear=True):
        icons = get_icons()
    assert isinstance(icons, UnicodeFallbackIcons)


def test_get_icons_ignores_false_value() -> None:
    """NERD_FONT=0 does not activate Nerd Font mode."""
    env = {k: v for k, v in os.environ.items() if k not in ("NERD_FONT", "BERNSTEIN_NERD_FONT")}
    env["NERD_FONT"] = "0"
    with patch.dict(os.environ, env, clear=True):
        icons = get_icons()
    assert isinstance(icons, UnicodeFallbackIcons)


# -- NerdFontIcons: agent icons ----------------------------------------------


def test_nerd_font_icons_has_claude_agent() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.agent_claude, str)
    assert len(icons.agent_claude) > 0


def test_nerd_font_icons_has_codex_agent() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.agent_codex, str)
    assert len(icons.agent_codex) > 0


def test_nerd_font_icons_has_gemini_agent() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.agent_gemini, str)
    assert len(icons.agent_gemini) > 0


def test_nerd_font_icons_has_cursor_agent() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.agent_cursor, str)
    assert len(icons.agent_cursor) > 0


# -- NerdFontIcons: status icons ---------------------------------------------


def test_nerd_font_icons_has_status_running() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.status_running, str)
    assert len(icons.status_running) > 0


def test_nerd_font_icons_has_status_done() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.status_done, str)
    assert len(icons.status_done) > 0


def test_nerd_font_icons_has_status_failed() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.status_failed, str)
    assert len(icons.status_failed) > 0


def test_nerd_font_icons_has_status_blocked() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.status_blocked, str)
    assert len(icons.status_blocked) > 0


# -- NerdFontIcons: quality gate icons ---------------------------------------


def test_nerd_font_icons_has_gate_lint() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.gate_lint, str)
    assert len(icons.gate_lint) > 0


def test_nerd_font_icons_has_gate_test() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.gate_test, str)
    assert len(icons.gate_test) > 0


def test_nerd_font_icons_has_gate_security() -> None:
    icons = NerdFontIcons()
    assert isinstance(icons.gate_security, str)
    assert len(icons.gate_security) > 0


# -- UnicodeFallbackIcons: same attributes exist -----------------------------


def test_unicode_fallback_has_all_agent_icons() -> None:
    icons = UnicodeFallbackIcons()
    for attr in ("agent_claude", "agent_codex", "agent_gemini", "agent_cursor"):
        val = getattr(icons, attr)
        assert isinstance(val, str) and len(val) > 0, f"{attr} missing or empty"


def test_unicode_fallback_has_all_status_icons() -> None:
    icons = UnicodeFallbackIcons()
    for attr in ("status_running", "status_done", "status_failed", "status_blocked"):
        val = getattr(icons, attr)
        assert isinstance(val, str) and len(val) > 0, f"{attr} missing or empty"


def test_unicode_fallback_has_all_gate_icons() -> None:
    icons = UnicodeFallbackIcons()
    for attr in ("gate_lint", "gate_test", "gate_security"):
        val = getattr(icons, attr)
        assert isinstance(val, str) and len(val) > 0, f"{attr} missing or empty"


# -- Nerd Font glyphs are different from Unicode fallbacks -------------------


def test_nerd_font_status_done_differs_from_unicode_fallback() -> None:
    """Nerd Font done icon is a different glyph than the Unicode fallback."""
    nf = NerdFontIcons()
    uf = UnicodeFallbackIcons()
    assert nf.status_done != uf.status_done


# -- get_agent_icon helper ---------------------------------------------------


def test_get_agent_icon_returns_icon_for_known_adapter() -> None:
    """get_agent_icon returns the correct icon for a known adapter name."""
    from bernstein.cli.icons import get_agent_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_agent_icon("claude")
    nf = NerdFontIcons()
    assert icon == nf.agent_claude


def test_get_agent_icon_returns_fallback_for_unknown_adapter() -> None:
    """get_agent_icon returns a non-empty string for unknown adapters."""
    from bernstein.cli.icons import get_agent_icon

    icon = get_agent_icon("unknown_adapter_xyz")
    assert isinstance(icon, str) and len(icon) > 0


# -- get_status_icon helper --------------------------------------------------


def test_get_status_icon_returns_done_for_done_status() -> None:
    from bernstein.cli.icons import get_status_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_status_icon("done")
    nf = NerdFontIcons()
    assert icon == nf.status_done


def test_get_status_icon_returns_fallback_for_unknown_status() -> None:
    from bernstein.cli.icons import get_status_icon

    icon = get_status_icon("some_unknown_status")
    assert isinstance(icon, str) and len(icon) > 0
