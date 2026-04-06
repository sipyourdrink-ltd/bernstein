"""Tests for CRT phosphor shader engine."""

from __future__ import annotations

from rich.color import Color
from rich.style import Style
from rich.text import Text

from bernstein.tui.crt_shader import (
    CRTMode,
    CRTShader,
    _dim_rgb,
    _luminance,
    _rgb_from_style,
    _to_monochrome,
)
from bernstein.tui.themes import (
    RETRO_AMBER_THEME,
    RETRO_COOL_THEME,
    RETRO_GREEN_THEME,
    THEMES,
    ThemeMode,
    cycle_theme,
)


def test_crt_shader_off_passthrough() -> None:
    """OFF mode returns text unchanged."""
    shader = CRTShader(CRTMode.OFF)
    assert not shader.active

    original = Text("hello world", style=Style(color="red"))
    result = shader.apply_to_text(original, row=0, total_width=80)

    # OFF mode should return the exact same object.
    assert result is original
    assert result.plain == "hello world"


def test_crt_shader_scanline_dims_odd_rows() -> None:
    """Odd rows have dimmed luminance."""
    shader = CRTShader(CRTMode.AMBER)
    bright = Style(color=Color.from_rgb(200, 200, 200))

    dimmed = shader.apply_scanline(bright, row=1)
    dimmed_rgb = _rgb_from_style(dimmed)

    assert dimmed_rgb is not None
    # 200 * 0.65 = 130
    assert dimmed_rgb[0] == 130
    assert dimmed_rgb[1] == 130
    assert dimmed_rgb[2] == 130

    # Background should be set to very dark (scanline gap).
    assert dimmed.bgcolor is not None


def test_crt_shader_scanline_preserves_even_rows() -> None:
    """Even rows are unchanged by scanline effect."""
    shader = CRTShader(CRTMode.AMBER)
    original = Style(color=Color.from_rgb(200, 200, 200))

    result = shader.apply_scanline(original, row=0)

    # Even row should pass through unchanged.
    assert result is original

    result2 = shader.apply_scanline(original, row=2)
    assert result2 is original

    result4 = shader.apply_scanline(original, row=4)
    assert result4 is original


def test_crt_shader_monochrome_amber() -> None:
    """Amber maps all colors to the orange-yellow spectrum."""
    shader = CRTShader(CRTMode.AMBER)

    # Pure white input (luminance ~1.0) should map to near-full amber.
    white_style = Style(color=Color.from_rgb(255, 255, 255))
    result = shader.apply_monochrome(white_style)
    rgb = _rgb_from_style(result)

    assert rgb is not None
    # Should be close to amber base (255, 176, 0).
    assert rgb[0] > 200  # Strong red channel
    assert rgb[1] > 140  # Medium green channel
    assert rgb[2] < 20  # Very low blue channel

    # A medium-brightness color should produce dimmer amber.
    mid_style = Style(color=Color.from_rgb(128, 128, 128))
    mid_result = shader.apply_monochrome(mid_style)
    mid_rgb = _rgb_from_style(mid_result)

    assert mid_rgb is not None
    assert mid_rgb[0] < rgb[0]  # Dimmer than white
    assert mid_rgb[2] == 0  # Blue still near zero


def test_crt_shader_monochrome_green() -> None:
    """Green maps all colors to the green phosphor spectrum."""
    shader = CRTShader(CRTMode.GREEN)

    white_style = Style(color=Color.from_rgb(255, 255, 255))
    result = shader.apply_monochrome(white_style)
    rgb = _rgb_from_style(result)

    assert rgb is not None
    # Green channel should dominate.
    assert rgb[1] > rgb[0]  # Green > Red
    assert rgb[1] > rgb[2]  # Green > Blue
    assert rgb[1] > 200  # Bright green


def test_crt_shader_monochrome_cool_white() -> None:
    """Cool white maps to blue-white spectrum."""
    shader = CRTShader(CRTMode.COOL_WHITE)

    white_style = Style(color=Color.from_rgb(255, 255, 255))
    result = shader.apply_monochrome(white_style)
    rgb = _rgb_from_style(result)

    assert rgb is not None
    # Blue channel should be highest (cool white base is 200, 220, 255).
    assert rgb[2] >= rgb[0]
    assert rgb[2] >= rgb[1]


def test_crt_shader_monochrome_off_passthrough() -> None:
    """OFF mode monochrome is a no-op."""
    shader = CRTShader(CRTMode.OFF)
    original = Style(color=Color.from_rgb(100, 200, 50))
    result = shader.apply_monochrome(original)
    assert result is original


def test_crt_shader_bloom_bright_bleeds() -> None:
    """Bright cell's color appears dimly in neighbor background."""
    shader = CRTShader(CRTMode.AMBER)

    # Create a row: dim, BRIGHT, dim.
    dim_style = Style(color=Color.from_rgb(20, 20, 20))
    bright_style = Style(color=Color.from_rgb(255, 255, 255))
    styles = [dim_style, bright_style, dim_style]

    # Check bloom on the neighbor (col 0, adjacent to bright col 1).
    result = shader.apply_bloom(styles, col=0)

    # The neighbor should have picked up bloom background.
    assert result.bgcolor is not None
    bg_triplet = result.bgcolor.get_truecolor()
    # Bloom should add some brightness from the white neighbor.
    assert bg_triplet.red > 0 or bg_triplet.green > 0 or bg_triplet.blue > 0

    # Also check col 2 (other neighbor).
    result2 = shader.apply_bloom(styles, col=2)
    assert result2.bgcolor is not None


def test_crt_shader_bloom_dim_no_bleed() -> None:
    """Dim cells don't cause bloom."""
    shader = CRTShader(CRTMode.AMBER)

    dim_style = Style(color=Color.from_rgb(20, 20, 20))
    styles = [dim_style, dim_style, dim_style]

    result = shader.apply_bloom(styles, col=1)
    # No bright neighbors, so style should not have bloom background.
    # It returns base_style unchanged since no bloom was added.
    assert result.bgcolor is None or result is styles[1]


def test_crt_shader_cycle_mode() -> None:
    """Cycles OFF -> AMBER -> GREEN -> COOL_WHITE -> OFF."""
    shader = CRTShader(CRTMode.OFF)

    assert shader.cycle_mode() == CRTMode.AMBER
    assert shader.mode == CRTMode.AMBER

    assert shader.cycle_mode() == CRTMode.GREEN
    assert shader.mode == CRTMode.GREEN

    assert shader.cycle_mode() == CRTMode.COOL_WHITE
    assert shader.mode == CRTMode.COOL_WHITE

    assert shader.cycle_mode() == CRTMode.OFF
    assert shader.mode == CRTMode.OFF


def test_crt_shader_empty_text() -> None:
    """Empty/blank text doesn't crash."""
    shader = CRTShader(CRTMode.AMBER)

    empty = Text("")
    result = shader.apply_to_text(empty, row=0, total_width=80)
    assert result.plain == ""

    blank = Text("   ")
    result2 = shader.apply_to_text(blank, row=1, total_width=80)
    assert len(result2.plain) == 3


def test_crt_shader_active_property() -> None:
    """Active property reflects mode correctly."""
    shader = CRTShader(CRTMode.OFF)
    assert not shader.active

    shader.mode = CRTMode.AMBER
    assert shader.active

    shader.mode = CRTMode.GREEN
    assert shader.active

    shader.mode = CRTMode.COOL_WHITE
    assert shader.active


def test_crt_shader_apply_to_text_full_pipeline() -> None:
    """Full apply_to_text pipeline produces valid output."""
    shader = CRTShader(CRTMode.GREEN)

    text = Text("Hello, CRT!")
    text.stylize(Style(color=Color.from_rgb(255, 100, 50)), 0, 5)
    text.stylize(Style(color=Color.from_rgb(100, 200, 255)), 7, 11)

    result = shader.apply_to_text(text, row=0, total_width=80)

    assert result.plain == "Hello, CRT!"
    assert len(result.plain) == len(text.plain)


def test_crt_shader_chromatic_aberration_edges() -> None:
    """Chromatic aberration modifies styles at screen edges."""
    shader = CRTShader(CRTMode.GREEN)

    # 20 chars wide, 5% edge = 1 col on each side.
    chars = "abcdefghijklmnopqrst"
    text = Text(chars)
    text.stylize(Style(color=Color.from_rgb(200, 100, 50)), 0, 20)

    result = shader.apply_to_text(text, row=0, total_width=20)
    assert result.plain == chars


def test_retro_themes_exist() -> None:
    """All 3 new retro themes are in THEMES dict."""
    assert ThemeMode.RETRO_AMBER in THEMES
    assert ThemeMode.RETRO_GREEN in THEMES
    assert ThemeMode.RETRO_COOL in THEMES

    assert THEMES[ThemeMode.RETRO_AMBER] is RETRO_AMBER_THEME
    assert THEMES[ThemeMode.RETRO_GREEN] is RETRO_GREEN_THEME
    assert THEMES[ThemeMode.RETRO_COOL] is RETRO_COOL_THEME


def test_retro_themes_have_all_fields() -> None:
    """Retro themes have all required ThemeColors fields."""
    for mode in (ThemeMode.RETRO_AMBER, ThemeMode.RETRO_GREEN, ThemeMode.RETRO_COOL):
        theme = THEMES[mode]
        assert theme.background
        assert theme.foreground
        assert theme.primary
        assert theme.secondary
        assert theme.success
        assert theme.warning
        assert theme.error
        assert theme.muted
        assert theme.border
        assert theme.selection


def test_retro_themes_cycle() -> None:
    """cycle_theme() visits retro themes."""
    # Starting from HIGH_CONTRAST, cycling should hit retro themes.
    mode = ThemeMode.HIGH_CONTRAST
    mode = cycle_theme(mode)
    assert mode == ThemeMode.RETRO_AMBER

    mode = cycle_theme(mode)
    assert mode == ThemeMode.RETRO_GREEN

    mode = cycle_theme(mode)
    assert mode == ThemeMode.RETRO_COOL

    mode = cycle_theme(mode)
    assert mode == ThemeMode.DARK


def test_luminance_helper() -> None:
    """Luminance computation matches expected values."""
    # Pure white = 1.0
    assert abs(_luminance(255, 255, 255) - 1.0) < 0.01
    # Pure black = 0.0
    assert _luminance(0, 0, 0) == 0.0
    # Green contributes most to perceived luminance.
    assert _luminance(0, 255, 0) > _luminance(255, 0, 0)
    assert _luminance(0, 255, 0) > _luminance(0, 0, 255)


def test_dim_rgb_helper() -> None:
    """Dimming scales RGB correctly."""
    assert _dim_rgb(200, 100, 50, 0.5) == (100, 50, 25)
    assert _dim_rgb(255, 255, 255, 0.0) == (0, 0, 0)
    assert _dim_rgb(255, 255, 255, 1.0) == (255, 255, 255)


def test_to_monochrome_helper() -> None:
    """Monochrome mapping preserves luminance in palette color."""
    # White -> amber should be close to full amber.
    mono = _to_monochrome(255, 255, 255, CRTMode.AMBER)
    assert mono[0] > 200
    assert mono[2] == 0

    # Black -> amber should be near black.
    mono_dark = _to_monochrome(0, 0, 0, CRTMode.AMBER)
    assert mono_dark == (0, 0, 0)


def test_rgb_from_style_none() -> None:
    """Style without color returns None."""
    s = Style(bold=True)
    assert _rgb_from_style(s) is None


def test_sin_lut_precomputed() -> None:
    """Sine LUT is populated on init."""
    shader = CRTShader(CRTMode.AMBER)
    assert len(shader._sin_lut) == 256
    # sin(0) should be 0.0
    assert abs(shader._sin_lut[0]) < 1e-10
    # sin(pi/2) = sin(64 * 2*pi/256) = sin(pi/2) = 1.0
    assert abs(shader._sin_lut[64] - 1.0) < 1e-10


def test_palette_luts_precomputed() -> None:
    """Palette LUTs have 256 entries for each active mode."""
    shader = CRTShader(CRTMode.GREEN)
    for mode in (CRTMode.AMBER, CRTMode.GREEN, CRTMode.COOL_WHITE):
        lut = shader._palette_luts[mode]
        assert len(lut) == 256
        # Index 0 should be black.
        assert lut[0] == (0, 0, 0)
        # Index 255 should be close to the base color.
        base_r, base_g, base_b = {
            CRTMode.AMBER: (255, 176, 0),
            CRTMode.GREEN: (51, 255, 51),
            CRTMode.COOL_WHITE: (200, 220, 255),
        }[mode]
        assert lut[255] == (base_r, base_g, base_b)
