"""Tests for ``scripts/gen_roadmap.py``.

The generator runs unattended on a schedule, so the properties worth
holding are the ones nobody will be watching for: that the render is
deterministic (otherwise the refresh opens a pull request every week
whether or not the roadmap moved), that undated milestones sort last
rather than crashing the date parse, and that splicing refuses a file
whose markers went missing instead of silently writing nothing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "gen_roadmap.py"
_spec = importlib.util.spec_from_file_location("gen_roadmap", _SCRIPT)
assert _spec and _spec.loader
gen_roadmap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_roadmap)


def _milestone(title: str, due: str | None, description: str = "Theme.") -> dict[str, Any]:
    return {
        "title": title,
        "due_on": due,
        "description": description,
        "html_url": f"https://github.com/o/r/milestone/{title}",
    }


class TestRender:
    def test_dated_milestones_come_first_in_date_order(self) -> None:
        out = gen_roadmap.render(
            [
                _milestone("v4.0.0", None),
                _milestone("v3.16.0", "2026-09-21T00:00:00Z"),
                _milestone("v3.15.0", "2026-08-24T00:00:00Z"),
            ]
        )
        assert out.index("v3.15.0") < out.index("v3.16.0") < out.index("v4.0.0")

    def test_undated_milestone_says_so_instead_of_guessing(self) -> None:
        out = gen_roadmap.render([_milestone("v4.0.0", None)])
        assert "scoped by content, no date" in out

    def test_due_date_is_rendered_in_full(self) -> None:
        out = gen_roadmap.render([_milestone("v3.15.0", "2026-08-24T00:00:00Z")])
        assert "due 24 August 2026" in out

    def test_render_is_deterministic(self) -> None:
        """Identical input renders identically, so an unchanged roadmap stays unchanged."""
        milestones = [_milestone("v3.15.0", "2026-08-24T00:00:00Z"), _milestone("v4.0.0", None)]
        assert gen_roadmap.render(milestones) == gen_roadmap.render(list(reversed(milestones)))

    def test_render_carries_no_timestamp(self) -> None:
        """A stamped render would differ on every run and defeat the change check."""
        out = gen_roadmap.render([_milestone("v3.15.0", "2026-08-24T00:00:00Z")])
        assert "generated on" not in out.lower()
        assert "2026-08-10" not in out

    def test_missing_description_is_marked_not_blank(self) -> None:
        out = gen_roadmap.render([_milestone("v9.9.9", None, description="")])
        assert "_No description on the milestone yet._" in out

    def test_no_open_milestones_renders_a_sentence(self) -> None:
        assert gen_roadmap.render([]) == "No open milestones."


class TestSplice:
    _DOC = f"intro\n\n{gen_roadmap.START}\n\nold\n\n{gen_roadmap.END}\n\noutro\n"

    def test_replaces_only_between_the_markers(self) -> None:
        out = gen_roadmap.splice(self._DOC, "new")
        assert "old" not in out
        assert out.startswith("intro")
        assert out.endswith("outro\n")
        assert "new" in out

    def test_splice_is_idempotent(self) -> None:
        once = gen_roadmap.splice(self._DOC, "new")
        assert gen_roadmap.splice(once, "new") == once

    def test_missing_markers_raise_rather_than_write_nothing(self) -> None:
        with pytest.raises(ValueError, match="markers"):
            gen_roadmap.splice("no markers here\n", "new")

    def test_reversed_markers_raise(self) -> None:
        broken = f"{gen_roadmap.END}\n{gen_roadmap.START}\n"
        with pytest.raises(ValueError, match="markers"):
            gen_roadmap.splice(broken, "new")
