"""FIGlet logo rendering for the premium Bernstein splash."""

from __future__ import annotations

from bernstein.cli.visual_theme import BERNSTEIN_GRADIENT, gradient_markup_lines

try:
    import pyfiglet
except ImportError:  # pragma: no cover - dependency fallback
    pyfiglet = None  # type: ignore[assignment]

DEFAULT_FALLBACK_FONTS: tuple[str, ...] = ("small", "standard", "mini")


def _render_font(text: str, font: str) -> str:
    """Render ``text`` with a specific FIGlet font.

    Args:
        text: Text to render.
        font: FIGlet font name.

    Returns:
        Unstyled FIGlet output.

    Raises:
        RuntimeError: If pyfiglet is unavailable.
        ValueError: If the requested font cannot render the text.
    """
    if pyfiglet is None:
        raise RuntimeError("pyfiglet is not available")
    try:
        figlet = pyfiglet.Figlet(font=font)
        return str(figlet.renderText(text))
    except (pyfiglet.FontNotFound, KeyError, ValueError) as exc:
        raise ValueError(f"FIGlet font unavailable: {font}") from exc


def _fits_width(rendered: str, max_width: int) -> bool:
    """Return True when every visible line fits within ``max_width``."""
    if max_width <= 0:
        return False
    lines = [line.rstrip() for line in rendered.splitlines()]
    return all(len(line) <= max_width for line in lines if line)


def _plain_fallback(text: str, color: str | None) -> str:
    """Return a plain-text fallback string."""
    if color:
        return f"[{color}]{text}[/]"
    return text


def _try_render_font(text: str, font_name: str, max_width: int) -> list[str] | None:
    """Attempt to render *text* with *font_name*, returning stripped lines or None."""
    try:
        rendered = _render_font(text, font_name)
    except (RuntimeError, ValueError):
        return None
    if not _fits_width(rendered, max_width):
        return None
    lines = [line.rstrip() for line in rendered.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines or None


def _first_successful_render(
    text: str,
    fonts: list[str],
    max_width: int,
) -> list[str] | None:
    """Try each font in order, returning the first successful render or None."""
    for candidate in fonts:
        lines = _try_render_font(text, candidate, max_width)
        if lines is not None:
            return lines
    return None


def render_logo(
    text: str = "BERNSTEIN",
    font: str = "slant",
    *,
    fallback_fonts: list[str] | None = None,
    max_width: int = 80,
    color: str | None = None,
) -> str:
    """Render text as a FIGlet logo with fallback fonts and gradient styling.

    Args:
        text: Display text.
        font: Preferred FIGlet font.
        fallback_fonts: Additional fonts tried if the primary font is missing or
            exceeds ``max_width``.
        max_width: Maximum permitted width for any rendered line.
        color: Optional Rich style applied to every line. When omitted, the
            Bernstein gradient is sampled across the logo lines.

    Returns:
        A Rich-markup string containing the rendered logo, or a plain-text
        fallback when no font is usable.
    """
    stripped = text.strip()
    if not stripped:
        return ""

    fonts_to_try: list[str] = [font]
    for candidate in fallback_fonts or list(DEFAULT_FALLBACK_FONTS):
        if candidate not in fonts_to_try:
            fonts_to_try.append(candidate)

    lines = _first_successful_render(stripped, fonts_to_try, max_width)
    if lines is None:
        return _plain_fallback(stripped, color)
    if color:
        style = color if "bold" in color else f"bold {color}"
        return "\n".join(f"[{style}]{line}[/]" if line else "" for line in lines)
    return gradient_markup_lines(lines, colors=BERNSTEIN_GRADIENT, style="bold")
