"""Tests for bernstein.cli.image_renderer and bernstein.cli.terminal_caps."""

from __future__ import annotations

import io
import time
from unittest.mock import patch

import pytest
from PIL import Image

from bernstein.cli.image_renderer import (
    BaseRenderer,
    BrailleRenderer,
    HalfBlockRenderer,
    ITerm2Renderer,
    KittyRenderer,
    NullRenderer,
    SixelRenderer,
    _encode_sixel,
    _make_renderer,
    render_image,
)
from bernstein.cli.terminal_caps import Protocol, TerminalCaps

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def red_image() -> Image.Image:
    """4×4 solid red image."""
    return Image.new("RGB", (4, 4), color=(255, 0, 0))


@pytest.fixture
def small_image() -> Image.Image:
    """8×8 gradient image with varied colors."""
    img = Image.new("RGB", (8, 8))
    for y in range(8):
        for x in range(8):
            img.putpixel((x, y), (x * 32, y * 32, 128))
    return img


def _caps(**overrides: object) -> TerminalCaps:
    """Build a TerminalCaps with sane defaults, applying *overrides*."""
    defaults: dict[str, object] = {
        "is_tty": True,
        "supports_truecolor": True,
        "supports_256color": True,
        "supports_kitty": False,
        "supports_iterm2": False,
        "supports_sixel": False,
        "term_width": 80,
        "term_height": 24,
    }
    defaults.update(overrides)
    return TerminalCaps(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def caps_kitty() -> TerminalCaps:
    return _caps(supports_kitty=True)


@pytest.fixture
def caps_iterm2() -> TerminalCaps:
    return _caps(supports_iterm2=True)


@pytest.fixture
def caps_sixel() -> TerminalCaps:
    return _caps(supports_sixel=True)


@pytest.fixture
def caps_truecolor() -> TerminalCaps:
    return _caps()


@pytest.fixture
def caps_null() -> TerminalCaps:
    return TerminalCaps.null()


# ── KittyRenderer ─────────────────────────────────────────────────────────


class TestKittyRenderer:
    def test_output_contains_apc_prefix(self, red_image: Image.Image) -> None:
        output = KittyRenderer().render(red_image, width=4, height=4)
        assert "\033_G" in output

    def test_output_contains_apc_terminator(self, red_image: Image.Image) -> None:
        output = KittyRenderer().render(red_image, width=4, height=4)
        assert "\033\\" in output

    def test_output_contains_a_T_param(self, red_image: Image.Image) -> None:
        """Kitty 'a=T' means 'transmit and display'."""
        output = KittyRenderer().render(red_image, width=4, height=4)
        assert "a=T" in output

    def test_final_chunk_m0(self, red_image: Image.Image) -> None:
        """The last APC chunk must have m=0 to signal end of transfer."""
        output = KittyRenderer().render(red_image, width=4, height=4)
        assert "m=0" in output

    def test_large_image_uses_continuation_chunks(self) -> None:
        """An image whose PNG exceeds 4096 bytes produces m=1 continuation chunks.

        Uses a large noisy image (random pixel data) so PNG compression cannot
        reduce the payload below the 4096-byte chunk boundary.
        """
        import random

        rng = random.Random(42)
        big = Image.new("RGB", (300, 300))
        big.putdata([(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)) for _ in range(300 * 300)])
        output = KittyRenderer().render(big, width=300, height=300)
        assert "m=1" in output
        assert "m=0" in output

    def test_format_param_is_png(self, red_image: Image.Image) -> None:
        """f=100 signals raw PNG format in the Kitty protocol."""
        output = KittyRenderer().render(red_image, width=4, height=4)
        assert "f=100" in output

    def test_output_ends_with_st(self, red_image: Image.Image) -> None:
        output = KittyRenderer().render(red_image, width=4, height=4)
        assert output.endswith("\033\\")


# ── ITerm2Renderer ────────────────────────────────────────────────────────


class TestITerm2Renderer:
    def test_output_contains_osc_1337(self, red_image: Image.Image) -> None:
        output = ITerm2Renderer().render(red_image, width=4, height=4)
        assert "\033]1337;" in output

    def test_output_contains_inline_flag(self, red_image: Image.Image) -> None:
        output = ITerm2Renderer().render(red_image, width=4, height=4)
        assert "inline=1" in output

    def test_output_ends_with_bel(self, red_image: Image.Image) -> None:
        output = ITerm2Renderer().render(red_image, width=4, height=4)
        assert output.endswith("\a")

    def test_output_contains_width_and_height(self, red_image: Image.Image) -> None:
        output = ITerm2Renderer().render(red_image, width=10, height=5)
        assert "width=10" in output
        assert "height=5" in output

    def test_output_contains_base64_payload(self, red_image: Image.Image) -> None:
        """There must be a non-empty base64 payload after the colon."""
        output = ITerm2Renderer().render(red_image, width=4, height=4)
        colon_pos = output.rfind(":")
        payload = output[colon_pos + 1 :].rstrip("\a")
        assert len(payload) > 10  # base64-encoded PNG is never this short


# ── SixelRenderer ─────────────────────────────────────────────────────────


class TestSixelRenderer:
    def test_output_starts_with_dcs(self, red_image: Image.Image) -> None:
        output = SixelRenderer().render(red_image, width=4, height=6)
        assert output.startswith("\x1bPq")

    def test_output_ends_with_st(self, red_image: Image.Image) -> None:
        output = SixelRenderer().render(red_image, width=4, height=6)
        assert output.endswith("\x1b\\")

    def test_output_contains_raster_attributes(self, red_image: Image.Image) -> None:
        """Raster attributes declare image dimensions."""
        output = SixelRenderer().render(red_image, width=4, height=6)
        assert '"1;1;' in output

    def test_output_contains_palette_entry(self, red_image: Image.Image) -> None:
        """Palette entries use the #N;2;R;G;B form."""
        output = SixelRenderer().render(red_image, width=4, height=6)
        assert ";2;" in output

    def test_output_contains_band_terminator(self, red_image: Image.Image) -> None:
        """'-' advances to the next sixel band."""
        output = SixelRenderer().render(red_image, width=4, height=6)
        assert "-" in output

    def test_encode_sixel_roundtrip_structure(self) -> None:
        """_encode_sixel produces valid DCS framing for a gradient image."""
        img = Image.new("RGB", (8, 12))
        for y in range(12):
            for x in range(8):
                img.putpixel((x, y), (x * 30, y * 20, 100))
        out = _encode_sixel(img)
        assert out.startswith("\x1bPq")
        assert out.endswith("\x1b\\")
        # One '-' per 6-row band: 12/6 = 2 bands
        assert out.count("-") >= 2


# ── HalfBlockRenderer ─────────────────────────────────────────────────────


class TestHalfBlockRenderer:
    def test_output_contains_background_truecolor(self, red_image: Image.Image) -> None:
        output = HalfBlockRenderer().render(red_image, width=4, height=2)
        assert "\033[48;2;" in output

    def test_output_contains_foreground_truecolor(self, red_image: Image.Image) -> None:
        output = HalfBlockRenderer().render(red_image, width=4, height=2)
        assert "\033[38;2;" in output

    def test_output_contains_half_block_char(self, red_image: Image.Image) -> None:
        output = HalfBlockRenderer().render(red_image, width=4, height=2)
        assert "\u2584" in output  # ▄

    def test_output_ends_with_reset_per_line(self, red_image: Image.Image) -> None:
        output = HalfBlockRenderer().render(red_image, width=4, height=2)
        assert "\033[0m" in output

    def test_correct_line_count(self, red_image: Image.Image) -> None:
        output = HalfBlockRenderer().render(red_image, width=4, height=2)
        assert len(output.split("\n")) == 2

    def test_red_image_encodes_red_channels(self) -> None:
        """A solid red image must contain 255;0;0 in the ANSI codes."""
        img = Image.new("RGB", (2, 4), color=(255, 0, 0))
        output = HalfBlockRenderer().render(img, width=2, height=2)
        assert "255;0;0" in output

    def test_single_row(self) -> None:
        img = Image.new("RGB", (4, 2), color=(0, 255, 0))
        output = HalfBlockRenderer().render(img, width=4, height=1)
        assert len(output.split("\n")) == 1
        assert "\u2584" in output

    def test_performance_80x48(self) -> None:
        """Half-block render of 80×48 characters must complete in <100 ms."""
        img = Image.new("RGB", (80, 96))
        for y in range(96):
            for x in range(80):
                img.putpixel((x, y), (x * 3, y * 2, 128))

        renderer = HalfBlockRenderer()
        t0 = time.perf_counter()
        renderer.render(img, width=80, height=48)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 100, f"Half-block render took {elapsed_ms:.1f} ms (limit: 100 ms)"


# ── BrailleRenderer ───────────────────────────────────────────────────────


class TestBrailleRenderer:
    def test_output_contains_braille_chars(self, red_image: Image.Image) -> None:
        output = BrailleRenderer().render(red_image, width=4, height=2)
        assert any(0x2800 <= ord(c) <= 0x28FF for c in output)

    def test_white_image_all_dots_set(self) -> None:
        """All-white image → all 8 braille dots set → U+28FF (⣿)."""
        img = Image.new("L", (4, 8), color=255)
        output = BrailleRenderer().render(img, width=2, height=2)
        assert "\u28ff" in output

    def test_black_image_no_dots(self) -> None:
        """All-black image → no dots set → U+2800 (empty braille)."""
        img = Image.new("L", (4, 8), color=0)
        output = BrailleRenderer().render(img, width=2, height=2)
        assert "\u2800" in output

    def test_correct_row_count(self) -> None:
        img = Image.new("L", (8, 16), color=200)
        output = BrailleRenderer().render(img, width=4, height=4)
        lines = output.split("\n")
        assert len(lines) == 4

    def test_correct_col_count(self) -> None:
        img = Image.new("L", (8, 16), color=200)
        output = BrailleRenderer().render(img, width=4, height=4)
        for line in output.split("\n"):
            assert len(line) == 4

    def test_threshold_boundary(self) -> None:
        """Pixels at exactly the threshold (128) are treated as lit."""
        img = Image.new("L", (2, 4), color=128)
        output = BrailleRenderer().render(img, width=1, height=1)
        # All pixels at threshold → lit → all bits set → U+28FF
        assert "\u28ff" in output

    def test_below_threshold_is_dark(self) -> None:
        """Pixels just below threshold (127) are dark."""
        img = Image.new("L", (2, 4), color=127)
        output = BrailleRenderer().render(img, width=1, height=1)
        assert "\u2800" in output


# ── NullRenderer ──────────────────────────────────────────────────────────


class TestNullRenderer:
    def test_returns_empty_string(self, red_image: Image.Image) -> None:
        assert NullRenderer().render(red_image, width=10, height=10) == ""


# ── _make_renderer ────────────────────────────────────────────────────────


class TestMakeRenderer:
    def test_kitty_caps_returns_kitty_renderer(self, caps_kitty: TerminalCaps) -> None:
        assert isinstance(_make_renderer(caps_kitty), KittyRenderer)

    def test_iterm2_caps_returns_iterm2_renderer(self, caps_iterm2: TerminalCaps) -> None:
        assert isinstance(_make_renderer(caps_iterm2), ITerm2Renderer)

    def test_sixel_caps_returns_sixel_renderer(self, caps_sixel: TerminalCaps) -> None:
        assert isinstance(_make_renderer(caps_sixel), SixelRenderer)

    def test_truecolor_caps_returns_halfblock_renderer(self, caps_truecolor: TerminalCaps) -> None:
        assert isinstance(_make_renderer(caps_truecolor), HalfBlockRenderer)

    def test_null_caps_returns_null_renderer(self, caps_null: TerminalCaps) -> None:
        assert isinstance(_make_renderer(caps_null), NullRenderer)

    def test_braille_caps_returns_braille_renderer(self) -> None:
        caps = _caps(is_tty=True, supports_truecolor=False, supports_256color=False)
        assert isinstance(_make_renderer(caps), BrailleRenderer)

    def test_all_renderers_are_base_renderer(self, caps_kitty: TerminalCaps, caps_null: TerminalCaps) -> None:
        """Every renderer must implement BaseRenderer."""
        for renderer in [
            KittyRenderer(),
            ITerm2Renderer(),
            SixelRenderer(),
            HalfBlockRenderer(),
            BrailleRenderer(),
            NullRenderer(),
        ]:
            assert isinstance(renderer, BaseRenderer)


# ── render_image ──────────────────────────────────────────────────────────


class TestRenderImage:
    def test_dispatches_to_half_block(self, red_image: Image.Image, caps_truecolor: TerminalCaps) -> None:
        out = io.StringIO()
        render_image(red_image, width=4, height=2, caps=caps_truecolor, file=out)
        assert "\u2584" in out.getvalue()

    def test_dispatches_to_kitty(self, red_image: Image.Image, caps_kitty: TerminalCaps) -> None:
        out = io.StringIO()
        render_image(red_image, width=4, height=4, caps=caps_kitty, file=out)
        assert "\033_G" in out.getvalue()

    def test_dispatches_to_iterm2(self, red_image: Image.Image, caps_iterm2: TerminalCaps) -> None:
        out = io.StringIO()
        render_image(red_image, width=4, height=4, caps=caps_iterm2, file=out)
        assert "\033]1337;" in out.getvalue()

    def test_dispatches_to_sixel(self, red_image: Image.Image, caps_sixel: TerminalCaps) -> None:
        out = io.StringIO()
        render_image(red_image, width=4, height=6, caps=caps_sixel, file=out)
        assert "\x1bPq" in out.getvalue()

    def test_null_caps_produces_empty_output(self, red_image: Image.Image, caps_null: TerminalCaps) -> None:
        out = io.StringIO()
        render_image(red_image, width=4, height=2, caps=caps_null, file=out)
        assert out.getvalue() == ""

    def test_synchronized_wrapping_on_tty(self, red_image: Image.Image, caps_truecolor: TerminalCaps) -> None:
        out = io.StringIO()
        render_image(red_image, width=4, height=2, caps=caps_truecolor, file=out, synchronized=True)
        result = out.getvalue()
        assert result.startswith("\033[?2026h")
        assert "\033[?2026l" in result

    def test_no_sync_wrapping_non_tty(self, red_image: Image.Image, caps_null: TerminalCaps) -> None:
        out = io.StringIO()
        render_image(red_image, width=4, height=2, caps=caps_null, file=out, synchronized=True)
        assert "\033[?2026h" not in out.getvalue()

    def test_no_sync_wrapping_when_disabled(self, red_image: Image.Image, caps_truecolor: TerminalCaps) -> None:
        out = io.StringIO()
        render_image(red_image, width=4, height=2, caps=caps_truecolor, file=out, synchronized=False)
        assert "\033[?2026h" not in out.getvalue()

    def test_output_written_to_file_param(self, red_image: Image.Image, caps_truecolor: TerminalCaps) -> None:
        """Output goes to the supplied file, not sys.stdout."""
        out = io.StringIO()
        render_image(red_image, width=4, height=2, caps=caps_truecolor, file=out)
        assert len(out.getvalue()) > 0

    def test_auto_detect_caps_when_none(self, red_image: Image.Image) -> None:
        """render_image must not raise when caps=None (auto-detection path)."""
        out = io.StringIO()
        # Patch is_tty to False so NullRenderer is chosen — avoids terminal queries
        with patch.object(TerminalCaps, "detect", return_value=TerminalCaps.null()):
            render_image(red_image, width=4, height=2, file=out)
        assert out.getvalue() == ""


# ── TerminalCaps ──────────────────────────────────────────────────────────


class TestTerminalCaps:
    def test_kitty_detected_by_env_var(self) -> None:
        env = {"KITTY_WINDOW_ID": "1", "COLORTERM": "truecolor", "TERM": "xterm-256color"}
        with patch.dict("os.environ", env, clear=False):
            caps = TerminalCaps.detect()
        assert caps.supports_kitty is True

    def test_iterm2_detected_by_term_program(self) -> None:
        env = {"TERM_PROGRAM": "iTerm.app", "COLORTERM": "truecolor"}
        with patch.dict("os.environ", env, clear=False):
            caps = TerminalCaps.detect()
        assert caps.supports_iterm2 is True

    def test_truecolor_detected(self) -> None:
        with patch.dict("os.environ", {"COLORTERM": "truecolor"}, clear=False):
            caps = TerminalCaps.detect()
        assert caps.supports_truecolor is True
        assert caps.supports_256color is True

    def test_24bit_colorterm_is_truecolor(self) -> None:
        with patch.dict("os.environ", {"COLORTERM": "24bit"}, clear=False):
            caps = TerminalCaps.detect()
        assert caps.supports_truecolor is True

    def test_null_caps(self) -> None:
        caps = TerminalCaps.null()
        assert caps.is_tty is False
        assert caps.supports_kitty is False
        assert caps.supports_iterm2 is False
        assert caps.supports_sixel is False
        assert caps.supports_truecolor is False
        assert caps.best_protocol is Protocol.NONE

    def test_best_protocol_kitty(self) -> None:
        caps = _caps(supports_kitty=True)
        assert caps.best_protocol is Protocol.KITTY

    def test_best_protocol_iterm2(self) -> None:
        caps = _caps(supports_iterm2=True)
        assert caps.best_protocol is Protocol.ITERM2

    def test_best_protocol_sixel(self) -> None:
        caps = _caps(supports_sixel=True)
        assert caps.best_protocol is Protocol.SIXEL

    def test_best_protocol_half_block(self) -> None:
        caps = _caps()
        assert caps.best_protocol is Protocol.HALF_BLOCK

    def test_best_protocol_braille_no_color(self) -> None:
        caps = _caps(supports_truecolor=False, supports_256color=False)
        assert caps.best_protocol is Protocol.BRAILLE

    def test_best_protocol_none_non_tty(self) -> None:
        caps = _caps(is_tty=False)
        assert caps.best_protocol is Protocol.NONE

    def test_kitty_beats_iterm2(self) -> None:
        caps = _caps(supports_kitty=True, supports_iterm2=True)
        assert caps.best_protocol is Protocol.KITTY

    def test_iterm2_beats_sixel(self) -> None:
        caps = _caps(supports_iterm2=True, supports_sixel=True)
        assert caps.best_protocol is Protocol.ITERM2

    def test_default_dimensions(self) -> None:
        caps = TerminalCaps.null()
        assert caps.term_width == 80
        assert caps.term_height == 24
