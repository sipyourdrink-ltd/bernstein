"""CRT phosphor shader engine for Bernstein's TUI.

Transforms Rich Text output to simulate a 1987 CRT monitor, including
scanline dimming, phosphor bloom, chromatic aberration at screen edges,
and monochrome phosphor palette mapping. Part of the "Bernstein '89"
retro demoscene aesthetic.
"""

from __future__ import annotations

import math
from enum import Enum

from rich.color import Color
from rich.console import Console
from rich.style import Style
from rich.text import Text

# Lightweight Console used only for resolving per-character styles.
_CONSOLE = Console()


class CRTMode(Enum):
    """CRT phosphor display modes."""

    OFF = "off"
    AMBER = "amber"
    GREEN = "green"
    COOL_WHITE = "cool_white"


# Ordered cycle for mode switching.
_MODE_CYCLE: list[CRTMode] = [
    CRTMode.OFF,
    CRTMode.AMBER,
    CRTMode.GREEN,
    CRTMode.COOL_WHITE,
]

# Base phosphor colors for each monochrome palette.
_PALETTE_BASE: dict[CRTMode, tuple[int, int, int]] = {
    CRTMode.AMBER: (255, 176, 0),
    CRTMode.GREEN: (51, 255, 51),
    CRTMode.COOL_WHITE: (200, 220, 255),
}

# Scanline dimming factor for odd rows (0.0 = black, 1.0 = full brightness).
_SCANLINE_DIM = 0.65

# Fraction of screen width at each edge affected by chromatic aberration.
_ABERRATION_EDGE = 0.05

# Bloom luminance threshold (characters brighter than this bleed glow).
_BLOOM_THRESHOLD = 0.7

# Bloom intensity factor applied to the glow color.
_BLOOM_INTENSITY = 0.25

# Sine LUT size.
_SIN_LUT_SIZE = 256


def _rgb_from_style(style: Style) -> tuple[int, int, int] | None:
    """Extract foreground RGB from a Rich Style.

    Args:
        style: Rich Style object.

    Returns:
        (r, g, b) tuple or None if no color is set.
    """
    if style.color is None:
        return None
    try:
        triplet = style.color.get_truecolor()
    except Exception:
        return None
    return (triplet.red, triplet.green, triplet.blue)


def _bg_rgb_from_style(style: Style) -> tuple[int, int, int] | None:
    """Extract background RGB from a Rich Style.

    Args:
        style: Rich Style object.

    Returns:
        (r, g, b) tuple or None if no bgcolor is set.
    """
    if style.bgcolor is None:
        return None
    try:
        triplet = style.bgcolor.get_truecolor()
    except Exception:
        return None
    return (triplet.red, triplet.green, triplet.blue)


def _luminance(r: int, g: int, b: int) -> float:
    """Compute perceived luminance of an RGB color.

    Uses the standard weighted formula for perceived brightness.

    Args:
        r: Red component (0-255).
        g: Green component (0-255).
        b: Blue component (0-255).

    Returns:
        Luminance in range 0.0 to 1.0.
    """
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _dim_rgb(r: int, g: int, b: int, factor: float) -> tuple[int, int, int]:
    """Multiply RGB components by a dimming factor.

    Args:
        r: Red component (0-255).
        g: Green component (0-255).
        b: Blue component (0-255).
        factor: Dimming factor (0.0 = black, 1.0 = unchanged).

    Returns:
        Dimmed (r, g, b) tuple.
    """
    return (
        int(r * factor + 0.5),
        int(g * factor + 0.5),
        int(b * factor + 0.5),
    )


def _blend_rgb(
    a: tuple[int, int, int],
    b: tuple[int, int, int],
    t: float,
) -> tuple[int, int, int]:
    """Linearly interpolate between two RGB colors.

    Args:
        a: First color.
        b: Second color.
        t: Blend factor (0.0 = a, 1.0 = b).

    Returns:
        Blended (r, g, b) tuple.
    """
    inv = 1.0 - t
    return (
        int(a[0] * inv + b[0] * t + 0.5),
        int(a[1] * inv + b[1] * t + 0.5),
        int(a[2] * inv + b[2] * t + 0.5),
    )


def _to_monochrome(  # pyright: ignore[reportUnusedFunction]
    r: int, g: int, b: int, palette: CRTMode
) -> tuple[int, int, int]:
    """Map an RGB color to a CRT phosphor monochrome color.

    Computes the perceived luminance, then scales the palette's base
    color by that luminance.

    Args:
        r: Red component (0-255).
        g: Green component (0-255).
        b: Blue component (0-255).
        palette: Target CRT mode (must not be OFF).

    Returns:
        Monochrome (r, g, b) tuple.
    """
    lum = _luminance(r, g, b)
    base = _PALETTE_BASE[palette]
    return _dim_rgb(base[0], base[1], base[2], lum)


class CRTShader:
    """CRT phosphor shader that transforms Rich Text for retro display.

    Applies scanline dimming, phosphor bloom, chromatic aberration, and
    monochrome palette mapping to simulate a vintage CRT monitor.

    Attributes:
        mode: Current CRT display mode.
    """

    def __init__(self, mode: CRTMode = CRTMode.OFF) -> None:
        self.mode = mode

        # Precompute sine LUT for bloom falloff.
        self._sin_lut: list[float] = [math.sin(i * 2.0 * math.pi / _SIN_LUT_SIZE) for i in range(_SIN_LUT_SIZE)]

        # Precompute monochrome palette LUTs (256 brightness levels -> RGB).
        self._palette_luts: dict[CRTMode, list[tuple[int, int, int]]] = {}
        for pmode, base in _PALETTE_BASE.items():
            lut: list[tuple[int, int, int]] = []
            for brightness in range(256):
                factor = brightness / 255.0
                lut.append(_dim_rgb(base[0], base[1], base[2], factor))
            self._palette_luts[pmode] = lut

    @property
    def active(self) -> bool:
        """Whether the shader is active (mode != OFF)."""
        return self.mode != CRTMode.OFF

    def cycle_mode(self) -> CRTMode:
        """Cycle to next CRT mode.

        Order: OFF -> AMBER -> GREEN -> COOL_WHITE -> OFF.

        Returns:
            The new mode after cycling.
        """
        idx = _MODE_CYCLE.index(self.mode)
        self.mode = _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]
        return self.mode

    def apply_scanline(self, style: Style, row: int) -> Style:
        """Dim odd rows to simulate CRT scanline gaps.

        Even rows (0, 2, 4...) pass through unchanged. Odd rows get
        foreground dimmed by ~35% and a very dark background applied.

        Args:
            style: Input Rich Style.
            row: Row number (0-indexed).

        Returns:
            Modified Style with scanline dimming on odd rows.
        """
        if row % 2 == 0:
            return style

        fg_rgb = _rgb_from_style(style)
        new_color = style.color
        if fg_rgb is not None:
            dimmed = _dim_rgb(*fg_rgb, _SCANLINE_DIM)
            new_color = Color.from_rgb(*dimmed)

        # Dark background to simulate the gap between phosphor lines.
        scanline_bg = Color.from_rgb(2, 2, 2)

        return Style(
            color=new_color,
            bgcolor=scanline_bg,
            bold=style.bold,
            italic=style.italic,
            underline=style.underline,
            strike=style.strike,
            overline=style.overline,
        )

    def apply_bloom(self, styles: list[Style], col: int) -> Style:
        """Apply phosphor bloom from bright neighboring cells.

        If an adjacent cell is bright (luminance > threshold), its color
        bleeds into this cell's background at reduced intensity, using a
        sine-based falloff.

        Args:
            styles: List of Styles for the entire row.
            col: Column index of the cell to apply bloom to.

        Returns:
            Style with bloom background applied, or the original style.
        """
        if not styles or col < 0 or col >= len(styles):
            return Style.null()

        base_style = styles[col]
        bloom_r, bloom_g, bloom_b = 0.0, 0.0, 0.0
        has_bloom = False

        # Check neighbors at distance 1 and 2.
        for offset in (-2, -1, 1, 2):
            neighbor_col = col + offset
            if neighbor_col < 0 or neighbor_col >= len(styles):
                continue

            neighbor_rgb = _rgb_from_style(styles[neighbor_col])
            if neighbor_rgb is None:
                continue

            lum = _luminance(*neighbor_rgb)
            if lum <= _BLOOM_THRESHOLD:
                continue

            # Sine-based falloff: closer neighbors contribute more.
            dist = abs(offset)
            lut_idx = int((dist / 3.0) * (_SIN_LUT_SIZE // 4)) % _SIN_LUT_SIZE
            falloff = max(0.0, 1.0 - self._sin_lut[lut_idx])
            intensity = _BLOOM_INTENSITY * falloff

            bloom_r += neighbor_rgb[0] * intensity
            bloom_g += neighbor_rgb[1] * intensity
            bloom_b += neighbor_rgb[2] * intensity
            has_bloom = True

        if not has_bloom:
            return base_style

        # Clamp and merge with existing background.
        glow = (
            min(255, int(bloom_r)),
            min(255, int(bloom_g)),
            min(255, int(bloom_b)),
        )

        existing_bg = _bg_rgb_from_style(base_style)
        final_bg = _blend_rgb(existing_bg, glow, 0.5) if existing_bg is not None else glow

        return Style(
            color=base_style.color,
            bgcolor=Color.from_rgb(*final_bg),
            bold=base_style.bold,
            italic=base_style.italic,
            underline=base_style.underline,
            strike=base_style.strike,
            overline=base_style.overline,
        )

    def apply_monochrome(self, style: Style) -> Style:
        """Map any color to the active CRT palette's monochrome.

        Uses precomputed LUT for fast brightness-to-palette lookup.

        Args:
            style: Input Rich Style.

        Returns:
            Style with foreground/background remapped to monochrome.
        """
        if self.mode == CRTMode.OFF:
            return style

        lut = self._palette_luts[self.mode]

        new_color = style.color
        fg_rgb = _rgb_from_style(style)
        if fg_rgb is not None:
            brightness = int(_luminance(*fg_rgb) * 255 + 0.5)
            brightness = min(255, max(0, brightness))
            mono = lut[brightness]
            new_color = Color.from_rgb(*mono)

        new_bgcolor = style.bgcolor
        bg_rgb = _bg_rgb_from_style(style)
        if bg_rgb is not None:
            brightness = int(_luminance(*bg_rgb) * 255 + 0.5)
            brightness = min(255, max(0, brightness))
            mono = lut[brightness]
            new_bgcolor = Color.from_rgb(*mono)

        return Style(
            color=new_color,
            bgcolor=new_bgcolor,
            bold=style.bold,
            italic=style.italic,
            underline=style.underline,
            strike=style.strike,
            overline=style.overline,
        )

    def _apply_chromatic_aberration(
        self,
        char_styles: list[tuple[str, Style]],
        total_width: int,
    ) -> list[tuple[str, Style]]:
        """Apply chromatic aberration at screen edges.

        At the leftmost and rightmost 5% of the screen, shifts red
        channel left by 1 cell and blue channel right by 1 cell to
        simulate CRT edge fringing.

        Args:
            char_styles: Per-character (char, style) pairs.
            total_width: Total terminal width.

        Returns:
            Modified char_styles with aberration applied at edges.
        """
        if total_width <= 0:
            return char_styles

        edge_cols = max(1, int(total_width * _ABERRATION_EDGE))
        n = len(char_styles)
        result = list(char_styles)

        for col in range(n):
            # Only apply at edges.
            if col >= edge_cols and col < total_width - edge_cols:
                continue

            fg_rgb = _rgb_from_style(char_styles[col][1])
            if fg_rgb is None:
                continue

            r, g, b = fg_rgb

            # Shift red channel left by 1, blue channel right by 1.
            # This cell gets green + shifted components from neighbors.
            red_src = col + 1  # red shifted left means source is to the right
            blue_src = col - 1  # blue shifted right means source is to the left

            new_r = r
            if 0 <= red_src < n:
                src_rgb = _rgb_from_style(char_styles[red_src][1])
                if src_rgb is not None:
                    new_r = src_rgb[0]

            new_b = b
            if 0 <= blue_src < n:
                src_rgb = _rgb_from_style(char_styles[blue_src][1])
                if src_rgb is not None:
                    new_b = src_rgb[2]

            old_style = char_styles[col][1]
            result[col] = (
                char_styles[col][0],
                Style(
                    color=Color.from_rgb(new_r, g, new_b),
                    bgcolor=old_style.bgcolor,
                    bold=old_style.bold,
                    italic=old_style.italic,
                    underline=old_style.underline,
                    strike=old_style.strike,
                    overline=old_style.overline,
                ),
            )

        return result

    def apply_to_text(self, text: Text, row: int, total_width: int) -> Text:
        """Apply all CRT effects to a line of Rich Text.

        Processing order: monochrome -> scanline -> bloom -> aberration.

        Args:
            text: The Rich Text object to transform.
            row: The row number (0-indexed, for scanline calculation).
            total_width: Total terminal width (for chromatic aberration).

        Returns:
            New Text object with CRT effects applied.
        """
        if not self.active:
            return text

        plain = text.plain
        if not plain:
            return text

        # Decompose text into per-character styles via public API.
        n = len(plain)
        char_styles: list[Style] = [text.get_style_at_offset(_CONSOLE, i) for i in range(n)]

        # 1. Monochrome mapping.
        char_styles = [self.apply_monochrome(s) for s in char_styles]

        # 2. Scanline dimming.
        char_styles = [self.apply_scanline(s, row) for s in char_styles]

        # 3. Phosphor bloom.
        bloomed: list[Style] = []
        for col in range(n):
            bloomed.append(self.apply_bloom(char_styles, col))
        char_styles = bloomed

        # 4. Chromatic aberration at edges.
        pairs = list(zip(plain, char_styles, strict=True))
        pairs = self._apply_chromatic_aberration(pairs, total_width)

        # Rebuild Text from per-character styles.
        result = Text()
        for char, style in pairs:
            result.append(char, style=style)

        return result
