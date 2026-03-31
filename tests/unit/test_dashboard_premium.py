"""Tests for premium dashboard visual helpers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from rich.console import Console

from bernstein.cli.dashboard import (
    DashboardHeader,
    _format_activity_line,
    _gradient_text,
    _priority_cell,
    _role_glyph,
)


def test_gradient_text_preserves_visible_label() -> None:
    rendered = _gradient_text("BERNSTEIN")

    assert rendered.plain == "BERNSTEIN"
    assert len(rendered.spans) == len("BERNSTEIN")


def test_priority_cell_highlights_p0() -> None:
    rendered = _priority_cell(0)

    assert rendered.plain == "P0"
    if rendered.spans:
        assert "bright_red" in str(rendered.spans[0].style)
    else:
        assert str(rendered.style) == "bold bright_red"


def test_role_glyph_returns_icons_for_known_roles() -> None:
    assert _role_glyph("backend")
    assert _role_glyph("qa")
    assert _role_glyph("manager")


def test_format_activity_line_adds_timestamp_role_and_error_highlight() -> None:
    rendered = _format_activity_line("backend", "ERROR: build failed")

    assert "[dim]" in rendered
    assert "BACKEND" in rendered
    assert "build failed" in rendered


def test_dashboard_header_render_contains_brand_and_cost() -> None:
    header = DashboardHeader()
    header.git_branch = "main"
    header.spent_usd = 1.25
    header.budget_usd = 5.00

    console = Console(record=True, width=120)
    console.print(header.render())
    output = console.export_text()

    assert "BERNSTEIN" in output
    assert "main" in output
    assert "$1.25/$5.00" in output
