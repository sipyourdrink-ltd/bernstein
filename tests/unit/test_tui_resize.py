"""Tests for TUI-001: terminal resize debounce.

Skipped: RESIZE_DEBOUNCE_S / _apply_resize not yet implemented on BernsteinApp.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bernstein.tui.app import BernsteinApp


@pytest.mark.skip(reason="TUI-001: resize debounce not yet implemented")
class TestResizeDebounce:
    """Tests for the resize debounce mechanism in BernsteinApp (TUI-001)."""

    def test_resize_debounce_constant_exists(self) -> None:
        """The debounce constant is defined and positive."""
        assert hasattr(BernsteinApp, "RESIZE_DEBOUNCE_S")
        assert BernsteinApp.RESIZE_DEBOUNCE_S > 0

    def test_resize_timer_initially_none(self) -> None:
        """The resize timer starts as None."""
        app = BernsteinApp()
        assert app._resize_timer is None

    def test_apply_resize_clears_timer(self) -> None:
        """_apply_resize sets _resize_timer back to None."""
        app = BernsteinApp()
        app._resize_timer = "sentinel"
        # Stub refresh to avoid needing a mounted app
        app.refresh = MagicMock()  # type: ignore[method-assign]
        app._apply_resize()
        assert app._resize_timer is None

    def test_apply_resize_calls_refresh(self) -> None:
        """_apply_resize calls refresh(layout=True)."""
        app = BernsteinApp()
        app.refresh = MagicMock()  # type: ignore[method-assign]
        app._apply_resize()
        app.refresh.assert_called_once_with(layout=True)

    def test_apply_resize_survives_layout_error(self) -> None:
        """_apply_resize catches layout errors without crashing."""
        app = BernsteinApp()
        app.refresh = MagicMock(side_effect=RuntimeError("layout calc failed"))  # type: ignore[method-assign]
        # Should NOT raise
        app._apply_resize()
        assert app._resize_timer is None

    def test_debounce_default_200ms(self) -> None:
        """Default debounce is 200ms."""
        assert pytest.approx(0.2) == BernsteinApp.RESIZE_DEBOUNCE_S


class TestDashboardResizeDebounce:
    """Tests for the resize debounce mechanism in the dashboard BernsteinApp (TUI-001)."""

    def test_dashboard_resize_debounce_constant(self) -> None:
        """The dashboard app also has a debounce constant."""
        from bernstein.cli.dashboard import BernsteinApp as DashboardApp

        assert hasattr(DashboardApp, "RESIZE_DEBOUNCE_S")
        assert DashboardApp.RESIZE_DEBOUNCE_S > 0

    def test_dashboard_resize_timer_initially_none(self) -> None:
        """The dashboard app starts with _resize_timer = None."""
        from bernstein.cli.dashboard import BernsteinApp as DashboardApp

        app = DashboardApp()
        assert app._resize_timer is None

    def test_dashboard_apply_resize_catches_errors(self) -> None:
        """Dashboard _apply_resize catches errors gracefully."""
        from bernstein.cli.dashboard import BernsteinApp as DashboardApp

        app = DashboardApp()
        app.refresh = MagicMock(side_effect=RuntimeError("layout error"))  # type: ignore[method-assign]
        app._apply_resize()
        assert app._resize_timer is None
