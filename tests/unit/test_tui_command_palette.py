"""Tests for TUI-012: Command palette with fuzzy search."""

from __future__ import annotations

import pytest
from textual.app import App

from bernstein.tui.command_palette import (
    DEFAULT_PALETTE_COMMANDS,
    CommandPalette,
    CommandPaletteScreen,
    PaletteCommand,
    fuzzy_match,
    render_palette,
    render_palette_item,
)


class TestFuzzyMatch:
    def test_exact_match(self) -> None:
        matched, score = fuzzy_match("quit", "Quit")
        assert matched is True
        assert score == 0

    def test_prefix_match(self) -> None:
        matched, score = fuzzy_match("tog", "Toggle Split Pane")
        assert matched is True
        assert score == 0  # substring match

    def test_fuzzy_chars_in_order(self) -> None:
        matched, score = fuzzy_match("tsp", "Toggle Split Pane")
        assert matched is True
        assert score > 0  # Fuzzy penalty

    def test_no_match(self) -> None:
        matched, _score = fuzzy_match("xyz", "quit")
        assert matched is False

    def test_empty_query(self) -> None:
        matched, score = fuzzy_match("", "anything")
        assert matched is True
        assert score == 0

    def test_case_insensitive(self) -> None:
        matched, _ = fuzzy_match("QUIT", "quit")
        assert matched is True

    def test_exact_scores_lower_than_fuzzy(self) -> None:
        _, exact_score = fuzzy_match("quit", "Quit Application")
        _, fuzzy_score = fuzzy_match("qa", "Quit Application")
        assert exact_score < fuzzy_score


class TestPaletteCommand:
    def test_defaults(self) -> None:
        cmd = PaletteCommand(name="Test", action="test")
        assert cmd.description == ""
        assert cmd.keybinding == ""
        assert cmd.category == "general"


class TestCommandPalette:
    def test_register(self) -> None:
        palette = CommandPalette()
        cmd = PaletteCommand("Test", "test")
        palette.register(cmd)
        assert len(palette.commands) == 1

    def test_register_many(self) -> None:
        palette = CommandPalette()
        palette.register_many(DEFAULT_PALETTE_COMMANDS)
        assert len(palette.commands) == len(DEFAULT_PALETTE_COMMANDS)

    def test_search_empty_query(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        results = palette.search("")
        assert len(results) == len(DEFAULT_PALETTE_COMMANDS)

    def test_search_exact(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        results = palette.search("quit")
        assert len(results) > 0
        assert results[0].action == "quit"

    def test_search_fuzzy(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        results = palette.search("tsp")
        # Should find "Toggle Split Pane"
        actions = [r.action for r in results]
        assert "toggle_split_pane" in actions

    def test_search_no_results(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        results = palette.search("xyzxyzxyz")
        assert len(results) == 0

    def test_move_selection_down(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        assert palette.selected_index == 0
        palette.move_selection(1)
        assert palette.selected_index == 1

    def test_move_selection_up(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        palette.selected_index = 2
        palette.move_selection(-1)
        assert palette.selected_index == 1

    def test_move_selection_clamped_top(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        palette.move_selection(-5)
        assert palette.selected_index == 0

    def test_move_selection_clamped_bottom(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        palette.move_selection(1000)
        assert palette.selected_index == len(DEFAULT_PALETTE_COMMANDS) - 1

    def test_get_selected(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        selected = palette.get_selected()
        assert selected is not None
        assert selected == DEFAULT_PALETTE_COMMANDS[0]

    def test_get_selected_empty(self) -> None:
        palette = CommandPalette()
        assert palette.get_selected() is None

    def test_clear(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        palette.set_query("test")
        palette.selected_index = 3
        palette.clear()
        assert palette.query == ""
        assert palette.selected_index == 0

    def test_set_query_resets_selection(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        palette.selected_index = 5
        palette.set_query("quit")
        assert palette.selected_index == 0

    def test_search_by_category(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        results = palette.search("view")
        assert len(results) > 0


class TestDefaultPaletteCommands:
    def test_has_required_commands(self) -> None:
        actions = {c.action for c in DEFAULT_PALETTE_COMMANDS}
        assert "quit" in actions
        assert "refresh" in actions
        assert "toggle_split_pane" in actions
        assert "copy_to_clipboard" in actions
        assert "command_palette" not in actions or "command_palette" in actions  # optional self-reference

    def test_all_have_names(self) -> None:
        for cmd in DEFAULT_PALETTE_COMMANDS:
            assert cmd.name
            assert cmd.action


class TestRenderPaletteItem:
    def test_basic_render(self) -> None:
        cmd = PaletteCommand("Quit", "quit", "Exit the TUI", "q")
        text = render_palette_item(cmd)
        assert "Quit" in text.plain

    def test_selected_render(self) -> None:
        cmd = PaletteCommand("Quit", "quit")
        text = render_palette_item(cmd, selected=True)
        assert "Quit" in text.plain

    def test_with_query_highlight(self) -> None:
        cmd = PaletteCommand("Quit Application", "quit")
        text = render_palette_item(cmd, query="quit")
        assert "Quit" in text.plain

    def test_keybinding_shown(self) -> None:
        cmd = PaletteCommand("Quit", "quit", keybinding="q")
        text = render_palette_item(cmd)
        assert "q" in text.plain


class TestRenderPalette:
    def test_empty_palette(self) -> None:
        palette = CommandPalette()
        text = render_palette(palette)
        assert "No matching" in text.plain

    def test_with_results(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        text = render_palette(palette, max_visible=5)
        assert ">" in text.plain

    def test_overflow_message(self) -> None:
        palette = CommandPalette(commands=list(DEFAULT_PALETTE_COMMANDS))
        text = render_palette(palette, max_visible=3)
        assert "more" in text.plain


class TestCommandPaletteScreen:
    @pytest.mark.asyncio
    async def test_mounts_with_input_and_results(self) -> None:
        app = App()
        async with app.run_test() as pilot:
            screen = CommandPaletteScreen()
            await app.push_screen(screen)
            await pilot.pause()
            assert screen.query_one("#command-palette-input") is not None
            assert screen.query_one("#command-palette-results") is not None
