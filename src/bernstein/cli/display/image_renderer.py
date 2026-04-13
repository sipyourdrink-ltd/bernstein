"""Universal image-to-terminal renderer.

Takes a PIL Image, detects terminal capabilities via TerminalCaps, and outputs
via the best available protocol:

    Kitty → iTerm2 → Sixel → Half-block → Braille → (none)

Each renderer is a self-contained class.  ``render_image()`` is the public
entry point — it selects the renderer, optionally wraps output in
synchronized-update mode (\\033[?2026h/l), and flushes to stdout or a caller-
supplied file object.

Performance target: <100 ms for an 80x48 character (80x96 pixel) image on the
half-block path (typically 10-30 ms in practice).
"""

from __future__ import annotations

import base64
import io
import sys
from abc import ABC, abstractmethod
from typing import Any, TextIO

from PIL import Image

from bernstein.cli.terminal_caps import Protocol, TerminalCaps

# ── Abstract base ──────────────────────────────────────────────────────────


class BaseRenderer(ABC):
    """Abstract interface for all image renderers."""

    @abstractmethod
    def render(self, img: Image.Image, width: int, height: int) -> str:
        """Render *img* at the given terminal dimensions and return the escape string.

        Args:
            img: Source PIL Image (any mode; renderers convert as needed).
            width: Target width in terminal columns.
            height: Target height in terminal rows.

        Returns:
            A string of ANSI/DCS/APC escape sequences ready to write to stdout.
        """
        ...


# ── Null renderer ──────────────────────────────────────────────────────────


class NullRenderer(BaseRenderer):
    """No-op renderer for non-TTY / dumb-terminal environments."""

    def render(self, img: Image.Image, width: int, height: int) -> str:
        return ""


# ── Kitty graphics protocol ────────────────────────────────────────────────


class KittyRenderer(BaseRenderer):
    """Kitty graphics protocol — chunked base64 PNG over APC sequences.

    Transmits PNG pixel data with no color quantization.  Large payloads are
    split into ≤4096-byte chunks; each chunk uses ``m=1`` to signal
    continuation, and the final chunk uses ``m=0`` to signal completion.

    Supported by: Kitty, WezTerm, Ghostty, and any terminal advertising
    ``KITTY_WINDOW_ID`` in the environment.

    Protocol reference: https://sw.kovidgoyal.net/kitty/graphics-protocol/
    """

    _CHUNK_SIZE = 4096

    def render(self, img: Image.Image, width: int, height: int) -> str:
        img = img.resize((width, height), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = base64.standard_b64encode(buf.getvalue())

        parts: list[str] = []
        while data:
            chunk, data = data[: self._CHUNK_SIZE], data[self._CHUNK_SIZE :]
            m = 1 if data else 0
            # APC sequence: ESC _ G <params> ; <base64-chunk> ESC \
            parts.append(f"\033_Ga=T,f=100,m={m};{chunk.decode('ascii')}\033\\")

        return "".join(parts)


# ── iTerm2 inline images ───────────────────────────────────────────────────


class ITerm2Renderer(BaseRenderer):
    """iTerm2 inline images via OSC 1337 — single-shot base64 PNG.

    One escape sequence carries the entire PNG payload.  Width/height are
    expressed in terminal columns/rows so the terminal handles scaling.

    Supported by: iTerm2 (native), WezTerm, VS Code terminal, Konsole, mintty.

    Protocol reference: https://iterm2.com/documentation-images.html
    """

    def render(self, img: Image.Image, width: int, height: int) -> str:
        img = img.resize((width, height), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        # OSC 1337 … BEL
        return f"\033]1337;File=inline=1;width={width};height={height};preserveAspectRatio=0:{b64}\a"


# ── Sixel DCS ──────────────────────────────────────────────────────────────


class SixelRenderer(BaseRenderer):
    """Sixel DCS output — PIL quantize → 256-color DCS sixel sequence.

    Quantizes the image to at most 256 indexed colors, then encodes as a
    compliant DCS sixel string with RLE compression.  The output size is
    roughly proportional to image area x color diversity.

    Supported by: xterm, Windows Terminal ≥ 1.23, VS Code ≥ 1.80, WezTerm,
    foot, iTerm2 ≥ 3.3, Konsole ≥ 22.04, mintty ≥ 2.6.
    """

    _MAX_COLORS = 256

    def render(self, img: Image.Image, width: int, height: int) -> str:
        img = img.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
        return _encode_sixel(img, max_colors=self._MAX_COLORS)


def _sixel_palette(pixels: list[int], palette: list[int]) -> list[str]:
    """Build sixel palette entries for colors present in the image."""
    out: list[str] = []
    for ci in sorted(set(pixels)):
        r = palette[ci * 3] * 100 // 255
        g = palette[ci * 3 + 1] * 100 // 255
        b = palette[ci * 3 + 2] * 100 // 255
        out.append(f"#{ci};2;{r};{g};{b}")
    return out


def _sixel_band(pixels: list[int], width: int, band_start: int, band_end: int) -> list[str]:
    """Encode a single 6-row sixel band with RLE compression."""
    color_cols: dict[int, list[int]] = {}
    for row_offset in range(band_end - band_start):
        y = band_start + row_offset
        bit = 1 << row_offset
        for x in range(width):
            ci = pixels[y * width + x]
            if ci not in color_cols:
                color_cols[ci] = [0] * width
            color_cols[ci][x] |= bit

    out: list[str] = []
    first = True
    for ci in sorted(color_cols.keys()):
        if not first:
            out.append("$")
        first = False
        out.append(f"#{ci}")
        out.extend(_rle_encode(color_cols[ci], width))
    out.append("-")
    return out


def _rle_encode(cols: list[int], width: int) -> list[str]:
    """RLE-encode a single color's sixel column data."""
    out: list[str] = []
    i = 0
    while i < width:
        bits = cols[i]
        run = 1
        while i + run < width and cols[i + run] == bits:
            run += 1
        char = chr(63 + bits)
        out.append(f"!{run}{char}" if run >= 4 else char * run)
        i += run
    return out


def _encode_sixel(img: Image.Image, max_colors: int = 256) -> str:
    """Encode a PIL RGB image as a DCS sixel escape string.

    The output is compliant with the DEC/VT340 sixel specification:

    - Raster attributes declare image dimensions.
    - Palette entries use the ``#N;2;R;G;B`` form (R/G/B in 0-100 range).
    - Sixel bands are 6 pixel rows tall; ``-`` advances to the next band.
    - ``$`` performs a carriage-return within a band (for multi-color output).
    - RLE: runs of >= 4 identical sixel characters are compressed as ``!N<c>``.

    Args:
        img: PIL RGB image.
        max_colors: Maximum palette size (1-256).

    Returns:
        DCS sixel escape string starting with ``\\x1bPq`` and ending with ST.
    """
    qimg = img.quantize(colors=max_colors, method=Image.Quantize.FASTOCTREE)
    palette: list[int] | None = qimg.getpalette()
    if palette is None:
        palette = [0] * (max_colors * 3)

    width, height = qimg.size
    pixels: list[int] = list(qimg.getdata())  # type: ignore[arg-type]

    out: list[str] = ["\x1bPq", f'"1;1;{width};{height}']
    out.extend(_sixel_palette(pixels, palette))

    for band_start in range(0, height, 6):
        band_end = min(band_start + 6, height)
        out.extend(_sixel_band(pixels, width, band_start, band_end))

    out.append("\x1b\\")
    return "".join(out)


# ── Half-block ─────────────────────────────────────────────────────────────


class HalfBlockRenderer(BaseRenderer):
    """Truecolor half-block renderer — U+2584 (▄) with 24-bit ANSI colors.

    Each terminal character cell covers **two** vertical pixels:

    - The **top** pixel maps to the cell's background color (``\\033[48;2;R;G;Bm``).
    - The **bottom** pixel maps to the foreground color (``\\033[38;2;R;G;Bm``).

    U+2584 (LOWER HALF BLOCK) fills only the lower half of the cell, so the
    two color channels are visually distinct.

    Effective resolution: ``width x (height x 2)`` pixels at 16.7 M colors.
    Best for photographic / gradient images.

    Performance: ~10-30 ms for 80x48 character output on a modern CPU.
    """

    def render(self, img: Image.Image, width: int, height: int) -> str:
        pixel_h = height * 2  # two pixel rows per character row
        img = img.convert("RGB").resize((width, pixel_h), Image.Resampling.LANCZOS)
        pixels = img.load()
        if pixels is None:  # pragma: no cover
            return ""

        lines: list[str] = []
        for y in range(0, pixel_h, 2):
            row: list[str] = []
            for x in range(width):
                r1, g1, b1 = pixels[x, y]  # top pixel → background
                r2, g2, b2 = pixels[x, y + 1]  # bottom pixel → foreground
                row.append(
                    f"\033[48;2;{r1};{g1};{b1}m\033[38;2;{r2};{g2};{b2}m\u2584"  # ▄
                )
            lines.append("".join(row) + "\033[0m")  # reset at end of line

        return "\n".join(lines)


# ── Braille ────────────────────────────────────────────────────────────────

# Braille dot layout within a 2x4 pixel cell (Unicode 8-dot Braille):
#
#   Col 0  Col 1
#   dot1   dot4   ← pixel row 0
#   dot2   dot5   ← pixel row 1
#   dot3   dot6   ← pixel row 2
#   dot7   dot8   ← pixel row 3
#
# Bit values (added to U+2800 base):
_BRAILLE_DOTS: list[list[int]] = [
    [0x01, 0x08],  # pixel row 0
    [0x02, 0x10],  # pixel row 1
    [0x04, 0x20],  # pixel row 2
    [0x40, 0x80],  # pixel row 3
]


class BrailleRenderer(BaseRenderer):
    """Braille character renderer — best for line art and monochrome content.

    Each Braille character covers a 2x4 pixel cell, yielding an effective
    resolution of ``(widthx2) x (heightx4)`` — e.g., 160x96 on an 80x24
    terminal.  The trade-off: only 2 colors per cell (no per-pixel color), so
    this renderer suits plots, line drawings, and monochrome icons rather than
    photographs.

    Pixels above *threshold* (default 128) are treated as lit (dot set).
    """

    _THRESHOLD: int = 128

    @staticmethod
    def _braille_bits(pixels: Any, cx: int, cy: int, pixel_w: int, pixel_h: int) -> int:
        """Compute the braille Unicode bits for a single cell position."""
        bits = 0
        for dy, dot_row in enumerate(_BRAILLE_DOTS):
            for dx, dot_bit in enumerate(dot_row):
                px_x = cx * 2 + dx
                px_y = cy * 4 + dy
                if px_x < pixel_w and px_y < pixel_h and pixels[px_x, px_y]:
                    bits |= dot_bit
        return bits

    def render(self, img: Image.Image, width: int, height: int) -> str:
        pixel_w = width * 2
        pixel_h = height * 4
        mono = (
            img.convert("L")
            .resize((pixel_w, pixel_h), Image.Resampling.LANCZOS)
            .point(lambda p: 255 if p >= self._THRESHOLD else 0)
        )
        pixels = mono.load()
        if pixels is None:  # pragma: no cover
            return ""

        lines: list[str] = []
        for cy in range(height):
            row = [chr(0x2800 + self._braille_bits(pixels, cx, cy, pixel_w, pixel_h)) for cx in range(width)]
            lines.append("".join(row))

        return "\n".join(lines)


# ── Renderer factory ───────────────────────────────────────────────────────


def _make_renderer(caps: TerminalCaps) -> BaseRenderer:
    """Return the appropriate renderer for the detected terminal capabilities."""
    protocol = caps.best_protocol
    match protocol:
        case Protocol.KITTY:
            return KittyRenderer()
        case Protocol.ITERM2:
            return ITerm2Renderer()
        case Protocol.SIXEL:
            return SixelRenderer()
        case Protocol.HALF_BLOCK:
            return HalfBlockRenderer()
        case Protocol.BRAILLE:
            return BrailleRenderer()
        case _:
            return NullRenderer()


# ── Public API ─────────────────────────────────────────────────────────────


def render_image(
    img: Image.Image,
    width: int,
    height: int,
    *,
    caps: TerminalCaps | None = None,
    file: TextIO | None = None,
    synchronized: bool = True,
) -> None:
    """Render a PIL Image to the terminal using the best available protocol.

    Dispatches to Kitty → iTerm2 → Sixel → HalfBlock → Braille based on
    detected (or supplied) terminal capabilities.  When writing to a live TTY
    and *synchronized* is True, wraps the output in synchronized-update mode
    (``\\033[?2026h`` / ``\\033[?2026l``) to eliminate tearing.

    Args:
        img: PIL Image to render.  Any mode; each renderer converts as needed.
        width: Target width in terminal columns.
        height: Target height in terminal rows.
        caps: Pre-detected :class:`TerminalCaps`.  Auto-detected when ``None``.
        file: Output file object.  Defaults to :data:`sys.stdout`.
        synchronized: Wrap output in synchronized-update mode on TTY.
            Has no effect when *caps.is_tty* is False.
    """
    if caps is None:
        caps = TerminalCaps.detect()

    out: TextIO = file if file is not None else sys.stdout
    renderer = _make_renderer(caps)
    output = renderer.render(img, width, height)

    if synchronized and caps.is_tty:
        out.write("\033[?2026h")  # begin synchronized update
        out.write(output)
        out.write("\033[?2026l")  # end synchronized update
    else:
        out.write(output)

    out.flush()
