"""The published terminal render has to keep matching what the code draws.

`docs/assets/tui-live.svg` is produced by driving the real `bernstein live`
dashboard against a committed fixture, so it is a claim about the current code
rather than a photograph of some past version. These tests are what keeps the
claim true: they re-render and compare, and they check the two properties the
comparison rests on - that the export is namespaced deterministically, and that
the fixture pins the clock.

The script under test is loaded via importlib (the pattern
`tests/unit/test_context_staleness.py` uses) so the gate and the test exercise
one implementation rather than two that agree today.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_tui_snapshot.py"
RENDER_PATH = REPO_ROOT / "docs" / "assets" / "tui-live.svg"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "tui_live_frame.json"


@pytest.fixture(scope="module")
def snapshot() -> Any:
    """Load scripts/render_tui_snapshot.py without executing main()."""
    spec = importlib.util.spec_from_file_location("render_tui_snapshot_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fixture_pins_the_clock() -> None:
    """Without a frozen instant every elapsed-time cell renders differently.

    This is the premise of the whole gate rather than an incidental field: a
    fixture without it produces a render that drifts once a second, which would
    turn the comparison into noise and get the check deleted.
    """
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(fixture.get("frozen_now"), (int, float))


def test_the_committed_render_carries_no_per_export_identifier(snapshot: Any) -> None:
    """Rich namespaces each export with a number that changes every process.

    It says nothing about what was drawn, so it is pinned to a constant. If a
    raw one ever lands in the committed file, byte comparison starts failing
    for a reason that has nothing to do with the dashboard.
    """
    committed = RENDER_PATH.read_text(encoding="utf-8")
    assert snapshot._STABLE_ID in committed
    assert not re.search(r"terminal-\d+", committed)


def test_the_committed_render_matches_what_the_dashboard_draws(snapshot: Any) -> None:
    """The gate itself: re-render the fixture and compare to what is published."""
    current = snapshot.render()
    committed = RENDER_PATH.read_text(encoding="utf-8")
    assert current == committed, (
        "docs/assets/tui-live.svg no longer matches the dashboard.\n"
        f"{snapshot.drift_report(committed, current)}\n"
        "Regenerate with: uv run python scripts/render_tui_snapshot.py --update"
    )


#: (label, text to edit, what to edit it to) per region. The same seeded task
#: title appears in both side-by-side panels - once as a row of the tasks
#: table, once inside the agent's log - which is exactly the case a row-only
#: attribution gets wrong, so the two are distinguished by their surroundings.
REGION_PROBES = (
    ("TASKS", ">&#160;Fix&#160;off-by-one", ">&#160;Fix&#160;off-by-two"),
    ("AGENTS", ">&#160;&#160;&#160;→&#160;Fix&#160;off-by-one", ">&#160;&#160;&#160;→&#160;Fix&#160;off-by-two"),
    ("ACTIVITY", "config&#160;reloaded", "config&#160;refreshed"),
)


@pytest.mark.parametrize(("region", "probe", "replacement"), REGION_PROBES, ids=[name for name, _, _ in REGION_PROBES])
def test_drift_is_attributed_to_the_region_it_happened_in(
    snapshot: Any, region: str, probe: str, replacement: str
) -> None:
    """ "The SVG differs" tells the reader nothing about where to look.

    The dashboard draws AGENTS and TASKS side by side, so a terminal row spans
    both: attribution has to read the column, not just the row, or a change in
    the tasks table gets reported against the agents panel.
    """
    committed = RENDER_PATH.read_text(encoding="utf-8")
    assert probe in committed, f"the {region} probe no longer matches the render"
    mutated = committed.replace(probe, replacement, 1)
    assert mutated != committed

    assert snapshot.regions_changed(mutated, committed) == [region]


def test_the_drift_report_shows_the_lines_and_not_only_the_region(snapshot: Any) -> None:
    """A region name alone still leaves the reader diffing an SVG by hand."""
    committed = RENDER_PATH.read_text(encoding="utf-8")
    mutated = committed.replace(">&#160;Fix&#160;off-by-one", ">&#160;Fix&#160;off-by-two", 1)

    report = snapshot.drift_report(mutated, committed)

    assert report.startswith("changed regions: TASKS")
    assert "off-by-one" in report, "the report should show the differing lines, not just name them"


def test_text_layer_recovers_the_rows_the_dashboard_drew(snapshot: Any) -> None:
    """The region names are only as good as the row reconstruction under them."""
    lines = snapshot.text_layer(RENDER_PATH.read_text(encoding="utf-8"))

    assert any("AGENTS" in line for line in lines)
    assert any("TASKS" in line for line in lines)
    # The fixture is a real demo frame: its seeded task titles have to survive
    # the round trip from SVG text nodes back into rows.
    assert any("get_item route" in line for line in lines)
