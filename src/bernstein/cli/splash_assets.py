"""Programmatic Pillow assets for the premium Bernstein splash."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from bernstein.cli.visual_theme import PALETTE, ROLE_COLORS, budget_color, hex_to_rgb, sample_gradient

if TYPE_CHECKING:
    from collections.abc import Sequence

FontType = ImageFont.ImageFont | ImageFont.FreeTypeFont

_FONT_PATHS: tuple[Path, ...] = (
    Path("/System/Library/Fonts/SFNS.ttf"),
    Path("/System/Library/Fonts/Supplemental/Helvetica.ttc"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/System/Library/Fonts/Supplemental/Menlo.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)

_FONT_NAMES: tuple[str, ...] = (
    "SF Pro Display",
    "Helvetica Neue",
    "Inter",
    "Arial",
    "DejaVuSans",
)


@lru_cache(maxsize=32)
def _load_font(size: int, bold: bool = False) -> FontType:
    """Load a preferred system font, falling back to Pillow's default font."""
    for path in _FONT_PATHS:
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    for name in _FONT_NAMES:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _vertical_gradient(width: int, height: int, colors: Sequence[str]) -> Image.Image:
    """Create an RGB vertical gradient image."""
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    gradient = sample_gradient(tuple(colors), height)
    for y, color in enumerate(gradient):
        draw.line((0, y, width, y), fill=hex_to_rgb(color), width=1)
    return img


def _draw_circuit_lines(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    """Draw subtle decorative lines for a circuit-board aesthetic."""
    line_color = hex_to_rgb(PALETTE.line)
    guides = (
        (int(width * 0.08), int(height * 0.18), int(width * 0.30), int(height * 0.18)),
        (int(width * 0.70), int(height * 0.22), int(width * 0.92), int(height * 0.22)),
        (int(width * 0.10), int(height * 0.76), int(width * 0.38), int(height * 0.76)),
        (int(width * 0.62), int(height * 0.74), int(width * 0.90), int(height * 0.74)),
    )
    for x0, y0, x1, y1 in guides:
        draw.line((x0, y0, x1, y1), fill=line_color, width=2)
        draw.ellipse((x0 - 3, y0 - 3, x0 + 3, y0 + 3), fill=line_color)
        draw.ellipse((x1 - 3, y1 - 3, x1 + 3, y1 + 3), fill=line_color)


def _draw_glow_text(
    base: Image.Image,
    *,
    position: tuple[int, int],
    text: str,
    font: FontType,
    fill: tuple[int, int, int],
    glow: tuple[int, int, int],
    blur_radius: int,
) -> None:
    """Draw glowing text by compositing blurred layers behind crisp text."""
    glow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    for alpha, offset in ((90, 0), (45, 2), (24, 4)):
        _draw_text(
            glow_draw,
            (position[0], position[1] + offset),
            text,
            font,
            (glow[0], glow[1], glow[2], alpha),
        )
    blurred = glow_layer.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    base.alpha_composite(blurred)

    crisp = ImageDraw.Draw(base)
    _draw_text(crisp, position, text, font, (*fill, 255))


def generate_splash_image(
    width: int = 800,
    height: int = 400,
    *,
    version: str = "",
    agent_count: int = 0,
) -> Image.Image:
    """Generate the premium splash hero image.

    Args:
        width: Output image width in pixels.
        height: Output image height in pixels.
        version: Optional version string shown in the metadata line.
        agent_count: Number of detected agents shown in the metadata line.

    Returns:
        RGBA Pillow image suitable for inline terminal rendering.
    """
    background = _vertical_gradient(width, height, (PALETTE.navy, PALETTE.navy_soft, PALETTE.teal)).convert("RGBA")
    draw = ImageDraw.Draw(background)
    _draw_circuit_lines(draw, width, height)

    title_font = _load_font(max(48, height // 5), bold=True)
    subtitle_font = _load_font(max(18, height // 16))
    meta_font = _load_font(max(14, height // 24))

    title = "BERNSTEIN"
    subtitle = "Agent Orchestra"
    metadata = f"{version or 'dev'}  •  {agent_count} agents online"

    title_bbox = _text_bbox(draw, title, title_font)
    title_width = title_bbox[2] - title_bbox[0]
    title_height = title_bbox[3] - title_bbox[1]
    title_pos = (max(24, (width - title_width) // 2), max(32, int(height * 0.20)))

    subtitle_bbox = _text_bbox(draw, subtitle, subtitle_font)
    subtitle_width = subtitle_bbox[2] - subtitle_bbox[0]
    subtitle_pos = (max(24, (width - subtitle_width) // 2), title_pos[1] + title_height + max(16, height // 24))

    _draw_glow_text(
        background,
        position=title_pos,
        text=title,
        font=title_font,
        fill=hex_to_rgb(PALETTE.text),
        glow=hex_to_rgb(PALETTE.cyan),
        blur_radius=max(4, height // 40),
    )
    draw = ImageDraw.Draw(background)
    _draw_text(draw, subtitle_pos, subtitle, subtitle_font, hex_to_rgb(PALETTE.glow))

    metadata_bbox = _text_bbox(draw, metadata, meta_font)
    metadata_pos = (
        width - (metadata_bbox[2] - metadata_bbox[0]) - 28,
        height - (metadata_bbox[3] - metadata_bbox[1]) - 20,
    )
    _draw_text(draw, metadata_pos, metadata, meta_font, hex_to_rgb(PALETTE.text_dim))

    accent_y = subtitle_pos[1] + max(24, height // 12)
    accent_color = hex_to_rgb(PALETTE.glow)
    draw.line((width * 0.18, accent_y, width * 0.82, accent_y), fill=accent_color, width=2)

    return background


def generate_agent_icon(role: str, status: str, size: int = 64) -> Image.Image:
    """Generate a circular agent icon with role fill and status ring."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    status_pct = 0.0 if status == "active" else 0.85 if status == "busy" else 1.0
    ring = hex_to_rgb(budget_color(status_pct))
    fill = hex_to_rgb(ROLE_COLORS.get(role.lower(), PALETTE.cyan))
    inner_margin = max(4, size // 10)

    draw.ellipse((2, 2, size - 2, size - 2), outline=ring, width=max(3, size // 12))
    draw.ellipse((inner_margin, inner_margin, size - inner_margin, size - inner_margin), fill=fill)

    label_font = _load_font(max(14, size // 3), bold=True)
    label = role[:1].upper()
    bbox = _text_bbox(draw, label, label_font)
    _draw_text(
        draw,
        ((size - (bbox[2] - bbox[0])) / 2, (size - (bbox[3] - bbox[1])) / 2 - 2),
        label,
        label_font,
        (255, 255, 255),
    )
    return image


def generate_progress_bar_image(width: int = 420, height: int = 18, *, progress: float = 1.0) -> Image.Image:
    """Generate a premium horizontal progress bar image."""
    pct = max(0.0, min(1.0, progress))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = height // 2
    border = hex_to_rgb(PALETTE.line)
    fill_gradient = sample_gradient((PALETTE.teal, PALETTE.cyan, PALETTE.glow), max(1, width))

    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=radius,
        outline=border,
        fill=hex_to_rgb(PALETTE.surface),
    )
    fill_width = max(0, min(width, int(width * pct)))
    for x in range(fill_width):
        draw.line((x, 2, x, height - 3), fill=hex_to_rgb(fill_gradient[min(x, len(fill_gradient) - 1)]), width=1)
    if fill_width > 0:
        draw.rounded_rectangle((0, 0, fill_width - 1, height - 1), radius=radius, outline=None, fill=None)
    return image


def _draw_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[float, float] | tuple[int, int],
    text: str,
    font: FontType,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
) -> None:
    """Typed wrapper around Pillow ``ImageDraw.text``."""
    draw.text(position, text, font=font, fill=fill)  # pyright: ignore[reportUnknownMemberType]


def _text_bbox(draw: ImageDraw.ImageDraw, text: str, font: FontType) -> tuple[int, int, int, int]:
    """Typed wrapper around Pillow ``ImageDraw.textbbox``."""
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)  # pyright: ignore[reportUnknownMemberType]
    return int(x0), int(y0), int(x1), int(y1)
