"""Tests for bernstein.cli.splash_assets."""

from __future__ import annotations

from bernstein.cli.splash_assets import (
    _load_font,
    generate_agent_icon,
    generate_progress_bar_image,
    generate_splash_image,
)
from PIL import ImageFont


def test_generate_splash_image_returns_expected_size_and_mode() -> None:
    image = generate_splash_image(width=320, height=180, version="1.2.3", agent_count=4)

    assert image.size == (320, 180)
    assert image.mode == "RGBA"


def test_generate_splash_image_is_not_blank() -> None:
    image = generate_splash_image(width=320, height=180)

    assert image.getbbox() is not None


def test_generate_agent_icon_returns_rgba_image() -> None:
    image = generate_agent_icon("backend", "active", size=48)

    assert image.size == (48, 48)
    assert image.mode == "RGBA"


def test_generate_agent_icon_has_visible_center_fill() -> None:
    image = generate_agent_icon("qa", "busy", size=48)

    center = image.getpixel((24, 24))
    assert center[3] > 0


def test_generate_progress_bar_image_respects_dimensions() -> None:
    image = generate_progress_bar_image(width=200, height=16, progress=0.5)

    assert image.size == (200, 16)


def test_load_font_falls_back_to_default_when_truetype_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _load_font.cache_clear()
    fallback = ImageFont.load_default()
    monkeypatch.setattr("PIL.ImageFont.truetype", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no font")))
    monkeypatch.setattr("PIL.ImageFont.load_default", lambda: fallback)

    font = _load_font(24)

    assert font is fallback
