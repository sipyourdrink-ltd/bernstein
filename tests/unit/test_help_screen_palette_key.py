"""The help screen's palette hint shows the resolved key, as its table does (#7874).

With ``command_palette: "ctrl+g"`` configured, the binding table showed Ctrl+G and the hint still
said to use Ctrl+P: the table read the resolved binding, the hint was a fixed string.
"""

from __future__ import annotations

import dataclasses

import pytest
from textual.app import App
from textual.widgets import DataTable

from bernstein.tui import keybinding_config
from bernstein.tui.help_screen import HelpScreen


def _bindings_with_palette(key: str) -> list[keybinding_config.KeyMapEntry]:
    entries = keybinding_config.resolve_all_bindings(json_path=None, yaml_path=None)
    return [dataclasses.replace(e, key=key) if e.action == "command_palette" else e for e in entries]


async def _shown(monkeypatch: pytest.MonkeyPatch, palette_key: str) -> tuple[str, list[str]]:
    entries = _bindings_with_palette(palette_key)
    monkeypatch.setattr(keybinding_config, "resolve_all_bindings", lambda *a, **k: entries)
    app = App()
    async with app.run_test() as pilot:
        screen = HelpScreen()
        await app.push_screen(screen)
        await pilot.pause()
        hints = str(screen.query_one("#help-hints").render())
        table = screen.query_one("#help-table", DataTable)
        keys = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        return hints, keys


@pytest.mark.asyncio
async def test_the_default_shows_ctrl_p_in_both_places(monkeypatch: pytest.MonkeyPatch) -> None:
    hints, keys = await _shown(monkeypatch, "ctrl+p")
    assert "Use Ctrl+P to search commands" in hints
    assert "Ctrl+P" in keys


@pytest.mark.asyncio
async def test_an_override_shows_the_same_key_in_both_places(monkeypatch: pytest.MonkeyPatch) -> None:
    hints, keys = await _shown(monkeypatch, "ctrl+g")
    assert "Use Ctrl+G to search commands" in hints
    assert "Ctrl+P" not in hints
    assert "Ctrl+G" in keys
    # The search shortcut is unchanged.
    assert "Use / to filter tasks" in hints
