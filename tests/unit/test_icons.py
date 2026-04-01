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


# -- _is_truthy direct tests -------------------------------------------------


def test_is_truthy_accepts_1() -> None:
    from bernstein.cli.icons import _is_truthy

    assert _is_truthy("1") is True


def test_is_truthy_accepts_true() -> None:
    from bernstein.cli.icons import _is_truthy

    assert _is_truthy("true") is True


def test_is_truthy_accepts_yes() -> None:
    from bernstein.cli.icons import _is_truthy

    assert _is_truthy("yes") is True


def test_is_truthy_accepts_on() -> None:
    from bernstein.cli.icons import _is_truthy

    assert _is_truthy("on") is True


def test_is_truthy_rejects_0() -> None:
    from bernstein.cli.icons import _is_truthy

    assert _is_truthy("0") is False


def test_is_truthy_rejects_false() -> None:
    from bernstein.cli.icons import _is_truthy

    assert _is_truthy("false") is False


def test_is_truthy_rejects_empty() -> None:
    from bernstein.cli.icons import _is_truthy

    assert _is_truthy("") is False


# -- get_icons with all truthy string variants --------------------------------


def test_get_icons_activates_on_true_string() -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("NERD_FONT", "BERNSTEIN_NERD_FONT")}
    env["NERD_FONT"] = "true"
    with patch.dict(os.environ, env, clear=True):
        icons = get_icons()
    assert isinstance(icons, NerdFontIcons)


def test_get_icons_activates_on_yes_string() -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("NERD_FONT", "BERNSTEIN_NERD_FONT")}
    env["NERD_FONT"] = "yes"
    with patch.dict(os.environ, env, clear=True):
        icons = get_icons()
    assert isinstance(icons, NerdFontIcons)


def test_get_icons_activates_on_on_string() -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("NERD_FONT", "BERNSTEIN_NERD_FONT")}
    env["BERNSTEIN_NERD_FONT"] = "on"
    with patch.dict(os.environ, env, clear=True):
        icons = get_icons()
    assert isinstance(icons, NerdFontIcons)


# -- get_agent_icon for all named adapters ------------------------------------


def test_get_agent_icon_codex() -> None:
    from bernstein.cli.icons import get_agent_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_agent_icon("codex")
    nf = NerdFontIcons()
    assert icon == nf.agent_codex


def test_get_agent_icon_gemini() -> None:
    from bernstein.cli.icons import get_agent_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_agent_icon("gemini")
    nf = NerdFontIcons()
    assert icon == nf.agent_gemini


def test_get_agent_icon_cursor() -> None:
    from bernstein.cli.icons import get_agent_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_agent_icon("cursor")
    nf = NerdFontIcons()
    assert icon == nf.agent_cursor


def test_get_agent_icon_is_case_insensitive() -> None:
    from bernstein.cli.icons import get_agent_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon_lower = get_agent_icon("claude")
        icon_upper = get_agent_icon("CLAUDE")
    assert icon_lower == icon_upper


# -- get_status_icon for all status aliases -----------------------------------


def test_get_status_icon_working_maps_to_running() -> None:
    from bernstein.cli.icons import get_status_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_status_icon("working")
    nf = NerdFontIcons()
    assert icon == nf.status_running


def test_get_status_icon_starting_maps_to_running() -> None:
    from bernstein.cli.icons import get_status_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_status_icon("starting")
    nf = NerdFontIcons()
    assert icon == nf.status_running


def test_get_status_icon_in_progress_maps_to_running() -> None:
    from bernstein.cli.icons import get_status_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_status_icon("in_progress")
    nf = NerdFontIcons()
    assert icon == nf.status_running


def test_get_status_icon_completed_maps_to_done() -> None:
    from bernstein.cli.icons import get_status_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_status_icon("completed")
    nf = NerdFontIcons()
    assert icon == nf.status_done


def test_get_status_icon_error_maps_to_failed() -> None:
    from bernstein.cli.icons import get_status_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_status_icon("error")
    nf = NerdFontIcons()
    assert icon == nf.status_failed


def test_get_status_icon_blocked_maps_to_blocked() -> None:
    from bernstein.cli.icons import get_status_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_status_icon("blocked")
    nf = NerdFontIcons()
    assert icon == nf.status_blocked


def test_get_status_icon_pending_approval_maps_to_blocked() -> None:
    from bernstein.cli.icons import get_status_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon = get_status_icon("pending_approval")
    nf = NerdFontIcons()
    assert icon == nf.status_blocked


def test_get_status_icon_is_case_insensitive() -> None:
    from bernstein.cli.icons import get_status_icon

    with patch.dict(os.environ, {"NERD_FONT": "1"}, clear=False):
        icon_lower = get_status_icon("done")
        icon_upper = get_status_icon("DONE")
    assert icon_lower == icon_upper


# -- Frozen dataclass — icon sets are immutable -------------------------------


def test_nerd_font_icons_are_immutable() -> None:
    """NerdFontIcons is a frozen dataclass; normal attribute assignment must raise."""
    import dataclasses

    import pytest

    nf = NerdFontIcons()
    assert dataclasses.fields(nf)  # is a dataclass
    with pytest.raises(dataclasses.FrozenInstanceError):
        nf.status_done = "x"  # type: ignore[misc]


def test_unicode_fallback_icons_are_immutable() -> None:
    """UnicodeFallbackIcons is a frozen dataclass; normal attribute assignment must raise."""
    import dataclasses

    import pytest

    uf = UnicodeFallbackIcons()
    with pytest.raises(dataclasses.FrozenInstanceError):
        uf.status_done = "x"  # type: ignore[misc]
