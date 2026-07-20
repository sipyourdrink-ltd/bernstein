"""Guard the block-art ASCII wordmark on the interactive startup banner.

The premium full-screen splash only fires on the bare ``bernstein`` invocation
and only in colour-capable terminals (tier1/tier2). Plain interactive terminals
(tier3) and ``bernstein run`` previously fell back to a compact one-liner / a
small box with no block-art, so the recognisable ASCII wordmark silently
disappeared for most operators. These tests pin the restored behaviour: an
interactive terminal shows the block-art; non-TTY output (CI, pipes, captured
test output) stays compact so logs are not polluted and the run-banner
regression guard keeps its ``Agent Orchestra`` marker.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from bernstein.cli.display.splash import _block_logo_lines, splash

_BLOCK_GLYPHS = ("▄", "▀", "█", "▐", "▌")


def _has_block_art(text: str) -> bool:
    return any(g in text for g in _BLOCK_GLYPHS)


def test_block_logo_asset_is_multiline_art() -> None:
    lines = _block_logo_lines()
    assert len(lines) > 1, "expected multi-line block-art logo asset"
    assert _has_block_art("\n".join(lines)), lines


def test_compact_splash_shows_block_art_on_a_tty() -> None:
    console = Console(force_terminal=True, width=100)
    with console.capture() as cap:
        splash(console, version="9.9.9", skip_animation=True)
    out = cap.get()
    assert _has_block_art(out), out
    assert "declarative agent orchestration" in out


def test_compact_splash_stays_compact_on_non_tty() -> None:
    # The CI / pipe shape must NOT emit block-art, keeping captured logs terse.
    console = Console(force_terminal=False, width=100)
    with console.capture() as cap:
        splash(console, version="9.9.9", skip_animation=True)
    out = cap.get()
    assert not _has_block_art(out), out
    assert "BERNSTEIN" in out


def test_compact_splash_stays_compact_on_narrow_tty() -> None:
    # Too narrow to hold the art: fall back to the one-liner even on a TTY.
    console = Console(force_terminal=True, width=30)
    with console.capture() as cap:
        splash(console, version="9.9.9", skip_animation=True)
    out = cap.get()
    assert not _has_block_art(out), out


def test_startup_banner_shows_block_art_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    import bernstein.cli.helpers as helpers

    tty_console = Console(force_terminal=True, width=100)
    monkeypatch.setattr(helpers, "console", tty_console)
    with tty_console.capture() as cap:
        helpers.print_startup_banner()
    out = cap.get()
    assert _has_block_art(out), out


def test_startup_banner_falls_back_to_box_on_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    import bernstein.cli.helpers as helpers

    plain_console = Console(force_terminal=False, width=100)
    monkeypatch.setattr(helpers, "console", plain_console)
    with plain_console.capture() as cap:
        helpers.print_startup_banner()
    out = cap.get()
    assert not _has_block_art(out), out
    assert "Agent Orchestra" in out
