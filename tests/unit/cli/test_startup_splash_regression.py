"""Regression coverage for the premium startup banner.

History: the premium splash (gradient + pixel-art logo + AGENT ORCHESTRA
subtitle) silently degrades to the compact banner under a handful of
conditions.  Two prior regressions (``6b1e16b32`` -- broken asset path
after the ``cli/display/`` reorg; ``fce779aaf`` -- complexity refactor)
both shipped because the bare ``except Exception`` in
``splash_screen.splash`` swallowed every renderer error and the user only
saw the compact fallback.

These tests pin three load-bearing invariants:

1. ``_load_logo`` resolves the ASCII logo asset in both the dev tree and
   the wheel layout (not the ``["  BERNSTEIN"]`` fallback).
2. ``SplashRenderer._render_premium`` under TTY-truecolor caps actually
   writes the logo glyphs and the ``AGENT ORCHESTRA`` signature to stdout.
3. ``splash_screen.splash`` falls back to compact when the premium
   renderer raises, but does so without obscuring the error in debug
   mode.
"""

from __future__ import annotations

import io
import os
import sys

import pytest
from rich.console import Console

from bernstein.cli.display.splash import _print_status_lines
from bernstein.cli.display.splash_screen import BootData, splash
from bernstein.cli.display.splash_screen import SplashRenderer as BootSplashRenderer
from bernstein.cli.display.splash_v2 import (
    SplashContext,
    SplashRenderer,
    _load_logo,
)
from bernstein.cli.display.terminal_caps import TerminalCaps
from bernstein.core.config.visual_config import VisualConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tty_caps(*, width: int = 120, height: int = 40) -> TerminalCaps:
    """Build TerminalCaps that simulate an interactive truecolor terminal."""
    return TerminalCaps(
        is_tty=True,
        supports_truecolor=True,
        supports_256color=True,
        supports_kitty=False,
        supports_iterm2=False,
        supports_sixel=False,
        term_width=width,
        term_height=height,
    )


# ---------------------------------------------------------------------------
# (1) Logo asset resolution
# ---------------------------------------------------------------------------


def test_load_logo_resolves_pixel_art_asset_not_fallback() -> None:
    """``_load_logo`` must find the real asset in dev or wheel layout.

    The hardcoded ``["  BERNSTEIN"]`` fallback only fires when the asset
    is missing in both candidate locations.  If we ever ship that
    fallback to users, the premium splash degrades to a plain text logo
    -- exactly the regression ``6b1e16b32`` fixed.
    """
    lines = _load_logo()

    assert lines != ["  BERNSTEIN"], (
        "logo asset not found in either dev or wheel layout; premium splash would degrade to plain text"
    )
    # The real asset is multi-line block-art; the fallback is a single
    # line.  Anything under 3 lines is suspicious enough to fail.
    assert len(lines) >= 3, f"logo unexpectedly short: {len(lines)} lines"

    # At least one line must contain block glyphs (the signature of the
    # pixel-art logo: ▄, █, ▀).
    block_chars = {"▄", "█", "▀"}
    assert any(any(ch in line for ch in block_chars) for line in lines), (
        f"logo lines contain no block glyphs: {lines!r}"
    )


# ---------------------------------------------------------------------------
# (2) Premium splash renders under interactive truecolor TTY
# ---------------------------------------------------------------------------


def test_premium_splash_emits_logo_and_subtitle_under_truecolor_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under TTY + truecolor caps, ``_render_premium`` writes the banner.

    Catches the case where the renderer silently produces an empty
    frame (logo glyph chars never reach stdout).  Uses a BytesIO sink
    in place of ``sys.stdout`` so the test stays headless and runs in
    well under 2 seconds (``skip_animation=True`` short-circuits the
    2.5s sleep).
    """
    # Replace stdout with an in-memory buffer for capture.  Rich's Console
    # has its own file handle; we patch ``sys.stdout`` directly because
    # ``_render_premium`` writes raw ANSI via ``sys.stdout.write``.
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    # Ensure CI gate inside SplashRenderer.render does NOT force tier3.
    monkeypatch.delenv("CI", raising=False)

    caps = _tty_caps()
    renderer = SplashRenderer(
        Console(file=buf, force_terminal=True),
        caps=caps,
        skip_animation=True,
        config=VisualConfig(splash=True, splash_tier="tier2"),
    )
    # Patch the post-render sleep so the test completes immediately.
    monkeypatch.setattr("bernstein.cli.display.splash_v2.time.sleep", lambda _s: None)

    renderer._render_premium(SplashContext(version="9.9.9"))

    output = buf.getvalue()

    # Signature 1: AGENT ORCHESTRA subtitle is unique to the premium
    # renderer.  The compact splash never emits this string.
    subtitle = "A G E N T   O R C H E S T R A"
    assert subtitle in output, (
        f"premium splash subtitle missing -- renderer likely fell through "
        f"to a no-op path; got {len(output)} bytes of output"
    )

    # Signature 2: at least one logo glyph reaches stdout.  An empty
    # render would have zero block-art chars.
    block_chars = {"▄", "█", "▀"}
    assert any(ch in output for ch in block_chars), "premium splash emitted no block-art glyphs"

    # Floor on output size catches the "renderer prints only escape
    # codes" failure mode (e.g. all writes routed to a discarded buf).
    assert len(output) >= 500, f"premium splash output too small: {len(output)} bytes"


# ---------------------------------------------------------------------------
# (3) Public splash() preserves fallback while exposing errors in debug mode
# ---------------------------------------------------------------------------


def test_splash_falls_back_to_compact_when_premium_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``splash()`` must keep startup alive even if premium renderer crashes."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated renderer failure")

    monkeypatch.setattr("bernstein.cli.display.splash_v2.render_startup_splash", _boom)
    monkeypatch.delenv("BERNSTEIN_DEBUG_SPLASH", raising=False)

    console = Console(force_terminal=True)
    # No exception escapes: fallback compact splash must run.
    splash(console, version="1.0", agents=[], skip_animation=True)

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "BERNSTEIN" in combined, f"compact fallback did not emit BERNSTEIN; got: {combined!r}"


def test_splash_debug_flag_surfaces_premium_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``BERNSTEIN_DEBUG_SPLASH=1`` must surface premium renderer failures.

    Locks in the load-bearing debug hook that lets future agents catch
    the "banner silently gone" regression without spelunking through
    rendering code.
    """

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated renderer failure")

    monkeypatch.setattr("bernstein.cli.display.splash_v2.render_startup_splash", _boom)
    monkeypatch.setenv("BERNSTEIN_DEBUG_SPLASH", "1")

    console = Console(force_terminal=True)
    splash(console, version="1.0", agents=[], skip_animation=True)

    err = capsys.readouterr().err
    assert "premium renderer failed" in err, f"debug hook did not log the premium failure; stderr: {err!r}"
    assert "simulated renderer failure" in err


# ---------------------------------------------------------------------------
# (4) Public splash() does not raise under headless / CI conditions
# ---------------------------------------------------------------------------


def test_splash_does_not_raise_in_headless_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``splash()`` must complete cleanly with no TTY (CI / piped stdout).

    Catches the failure mode where a guarded code path raises on
    non-interactive stdout (the "no TTY -> AttributeError" class of
    bug).  We intentionally do NOT assert on output content here; the
    point is purely that startup never aborts.
    """
    monkeypatch.setenv("CI", "1")
    # Make stdout look non-interactive.
    console = Console(file=io.StringIO(), force_terminal=False)

    # Must not raise.
    splash(console, version="1.0", agents=[], skip_animation=True)


# ---------------------------------------------------------------------------
# (5) Module alias from main.py:753 still resolves
# ---------------------------------------------------------------------------


def test_main_module_alias_resolves_to_splash_callable() -> None:
    """``from bernstein.cli.splash_screen import splash`` must keep working.

    ``main.py`` imports the splash entry point from the legacy path
    rather than the post-reorg ``bernstein.cli.display.splash_screen``
    location.  If the lazy alias in ``bernstein.cli.__init__`` is ever
    dropped, the TUI startup banner disappears entirely.
    """
    # Import via the alias path used by main.py.
    from bernstein.cli.splash_screen import splash as aliased_splash

    assert callable(aliased_splash)
    # The aliased module exposes the same callable as the canonical one.
    assert aliased_splash is splash or aliased_splash.__name__ == "splash"


# ---------------------------------------------------------------------------
# (6) main.py imports the splash callable from the expected path
# ---------------------------------------------------------------------------


def test_main_cli_imports_splash_from_aliased_path() -> None:
    """Pin the exact import that main.py:753 performs.

    A future move of ``splash_screen`` away from the lazy alias map
    would silently fail to find the symbol; this test fails first.
    """
    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "..",
        "src",
        "bernstein",
        "cli",
        "main.py",
    )
    with open(source, encoding="utf-8") as f:
        text = f.read()
    assert "from bernstein.cli.splash_screen import splash" in text, (
        "main.py no longer imports splash from the expected alias path; TUI startup banner will regress"
    )


# ---------------------------------------------------------------------------
# (7) Narrow terminals drop the goal preview instead of slicing from the end
# ---------------------------------------------------------------------------


def test_compact_splash_truncates_goal_to_zero_on_narrow_terminal() -> None:
    """A width below the label budget must print no goal characters."""
    goal = "Z" * 60
    buf = io.StringIO()
    console = Console(file=buf, width=10)

    _print_status_lines(
        console,
        width=10,
        seed_file=None,
        goal_preview=goal,
        budget=0.0,
        task_count=0,
    )

    output = buf.getvalue()
    assert "goal" in output
    assert "Z" not in output
    assert len([line for line in output.splitlines() if line.strip()]) == 1


def test_premium_splash_truncates_goal_to_zero_on_narrow_terminal() -> None:
    """The premium renderer must not leak a negative-sliced goal."""
    data = BootData(goal_preview="Z" * 60)
    renderer = BootSplashRenderer(data, width=19, height=40)

    renderer.phase3_status(0.0)
    output = renderer.render().plain

    assert "Goal:" in output
    assert "Z" not in output
