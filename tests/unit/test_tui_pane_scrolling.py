"""Regression coverage for scrolling overflowing TUI pane columns."""

from __future__ import annotations

import pytest
from textual.containers import VerticalScroll

from bernstein.tui.app import BernsteinApp
from bernstein.tui.layout_persistence import preset_layout


@pytest.mark.asyncio
async def test_overflowing_pane_columns_scroll_to_their_final_child() -> None:
    """Each pane uses one scroll viewport for all of its ordered widgets."""
    app = BernsteinApp(poll_interval=60.0)
    app._layout = preset_layout("observability")

    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()

        for pane_id in ("left-pane", "right-pane"):
            pane = app.query_one(f"#{pane_id}", VerticalScroll)
            assert pane.max_scroll_y > 0

            final_child = next(child for child in reversed(pane.children) if child.display)
            pane.scroll_end(animate=False)
            await pilot.pause()

            assert pane.scroll_y == pane.max_scroll_y
            assert final_child.region.bottom <= pane.content_region.bottom
