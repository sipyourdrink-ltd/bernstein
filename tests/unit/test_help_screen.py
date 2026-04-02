"""Tests for help screen modal."""

from __future__ import annotations

import pytest
from textual.app import App

from bernstein.tui.help_screen import HelpScreen


class TestHelpScreen:
    """Test HelpScreen modal."""

    def test_help_screen_creation(self) -> None:
        """Test help screen can be created."""
        screen = HelpScreen()

        assert screen is not None

    def test_help_screen_bindings(self) -> None:
        """Test help screen has correct bindings."""
        screen = HelpScreen()

        # Should have escape and q bindings
        binding_keys = [b[0] if isinstance(b, tuple) else b.key for b in screen.BINDINGS]
        assert "escape" in binding_keys
        assert "q" in binding_keys

    @pytest.mark.asyncio
    async def test_help_screen_mount(self) -> None:
        """Test help screen mounts correctly."""
        app = App()
        async with app.run_test() as pilot:
            screen = HelpScreen()
            await app.push_screen(screen)
            # Wait for any mount events
            await pilot.pause()

            # Should have table and title
            assert screen.query_one("#help-table") is not None
            assert screen.query_one("#help-title") is not None

    @pytest.mark.asyncio
    async def test_help_screen_populated(self) -> None:
        """Test help screen table is populated."""
        app = App()
        async with app.run_test() as pilot:
            screen = HelpScreen()
            await app.push_screen(screen)
            await pilot.pause()

            table = screen.query_one("#help-table")
            # Should have rows
            assert table.row_count > 0

    @pytest.mark.asyncio
    async def test_help_screen_dismiss(self) -> None:
        """Test help screen can be dismissed."""
        app = App()
        async with app.run_test() as pilot:
            screen = HelpScreen()
            await app.push_screen(screen)
            await pilot.pause()

            # Dismiss should work
            screen.dismiss()
            await pilot.pause()
            # screen is removed from stack
            assert screen not in app.screen_stack
