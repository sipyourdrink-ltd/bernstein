"""Tests for bernstein.cli.figlet_logo."""

from __future__ import annotations

import pytest
from bernstein.cli.figlet_logo import render_logo


def test_render_logo_returns_non_empty_string() -> None:
    rendered = render_logo("BERNSTEIN", max_width=120)

    assert isinstance(rendered, str)
    assert rendered.strip()


def test_render_logo_falls_back_when_primary_font_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_render(text: str, font: str) -> str:
        calls.append(font)
        if font == "slant":
            raise ValueError("missing")
        return "OK\n"

    monkeypatch.setattr("bernstein.cli.figlet_logo._render_font", fake_render)

    rendered = render_logo("BERNSTEIN", font="slant", fallback_fonts=["small"], max_width=80)

    assert calls == ["slant", "small"]
    assert "OK" in rendered


def test_render_logo_skips_fonts_that_exceed_max_width(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_render(text: str, font: str) -> str:
        if font == "slant":
            return "X" * 120
        return "FIT\n"

    monkeypatch.setattr("bernstein.cli.figlet_logo._render_font", fake_render)

    rendered = render_logo("BERNSTEIN", font="slant", fallback_fonts=["small"], max_width=20)

    assert "FIT" in rendered


def test_render_logo_returns_plain_text_when_all_fonts_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_render(text: str, font: str) -> str:
        raise ValueError("bad font")

    monkeypatch.setattr("bernstein.cli.figlet_logo._render_font", fake_render)

    assert render_logo("BERNSTEIN", fallback_fonts=["foo", "bar"]) == "BERNSTEIN"


def test_render_logo_uses_explicit_color_instead_of_gradient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bernstein.cli.figlet_logo._render_font", lambda text, font: "TOP\nBOTTOM\n")

    rendered = render_logo("BERNSTEIN", color="bold cyan")

    assert "[bold cyan]TOP[/]" in rendered
    assert "[bold cyan]BOTTOM[/]" in rendered


def test_render_logo_returns_empty_for_blank_text() -> None:
    assert render_logo("   ") == ""
