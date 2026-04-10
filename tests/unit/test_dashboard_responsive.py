"""Tests that the dashboard HTML contains mobile-responsive classes and meta tags."""

from __future__ import annotations

from pathlib import Path

import pytest

# Path to the dashboard template
_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "bernstein"
    / "dashboard"
    / "templates"
    / "index.html"
)


@pytest.fixture()
def html_content() -> str:
    """Read the dashboard HTML template."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


class TestViewportMeta:
    """The page must include a proper viewport meta tag for mobile rendering."""

    def test_viewport_meta_present(self, html_content: str) -> None:
        assert 'name="viewport"' in html_content

    def test_viewport_has_width_device_width(self, html_content: str) -> None:
        assert "width=device-width" in html_content

    def test_viewport_has_initial_scale(self, html_content: str) -> None:
        assert "initial-scale=1" in html_content


class TestResponsiveMediaQueries:
    """The <style> block must contain @media breakpoints for mobile layouts."""

    def test_media_query_768(self, html_content: str) -> None:
        assert "@media (max-width: 768px)" in html_content

    def test_media_query_480(self, html_content: str) -> None:
        assert "@media (max-width: 480px)" in html_content


class TestResponsiveGridClasses:
    """The layout must use Tailwind responsive grid classes."""

    def test_stat_cards_responsive_grid(self, html_content: str) -> None:
        # Stat cards should use grid-cols-1 sm:grid-cols-2 md:grid-cols-4
        assert "grid-cols-1 sm:grid-cols-2 md:grid-cols-4" in html_content

    def test_main_layout_responsive_grid(self, html_content: str) -> None:
        # Main area should use grid-cols-1 lg:grid-cols-5
        assert "grid-cols-1 lg:grid-cols-5" in html_content

    def test_bottom_section_responsive_grid(self, html_content: str) -> None:
        # Bottom section should use responsive grid
        assert "sm:grid-cols-2 lg:grid-cols-4" in html_content


class TestTaskTableScrollable:
    """The task table must be horizontally scrollable on small screens."""

    def test_table_wrapper_overflow_x_auto(self, html_content: str) -> None:
        assert "overflow-x-auto" in html_content

    def test_table_min_width(self, html_content: str) -> None:
        # Table should have a minimum width so it scrolls rather than squishes
        assert "min-w-[600px]" in html_content


class TestMobileMenuToggle:
    """A mobile menu toggle button must exist for the sidebar."""

    def test_mobile_toggle_button_exists(self, html_content: str) -> None:
        assert "Toggle sidebar" in html_content

    def test_mobile_toggle_hidden_on_desktop(self, html_content: str) -> None:
        # The toggle button should be hidden on large screens
        assert "lg:hidden" in html_content

    def test_sidebar_responsive_visibility(self, html_content: str) -> None:
        # Sidebar should use hidden lg:flex pattern when collapsed
        assert "hidden lg:flex" in html_content


class TestSidebarState:
    """The Alpine.js data must include sidebarOpen state."""

    def test_sidebar_open_in_alpine_data(self, html_content: str) -> None:
        assert "sidebarOpen" in html_content


class TestResponsiveUtilities:
    """Responsive utility classes are used for hiding elements on mobile."""

    def test_hidden_sm_inline_used(self, html_content: str) -> None:
        # Some elements should be hidden on very small screens
        assert "hidden sm:inline" in html_content

    def test_hidden_md_inline_used(self, html_content: str) -> None:
        assert "hidden md:inline" in html_content

    def test_filter_bar_class(self, html_content: str) -> None:
        # Filter bar should have the responsive class
        assert "filter-bar" in html_content


class TestDarkThemePreserved:
    """The dark theme must still be intact after responsive changes."""

    def test_dark_background(self, html_content: str) -> None:
        assert "background: #0a0a0b" in html_content

    def test_dark_surface_color(self, html_content: str) -> None:
        assert "surface: '#1a1a1d'" in html_content

    def test_dark_text_color(self, html_content: str) -> None:
        assert "color: #e5e5e5" in html_content
