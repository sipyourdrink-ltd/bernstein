"""Unit tests for the observability trends renderer.

Covers ``scripts/observability/render_trends.py`` against #3954's finding:
the renderer's window line and caption text assumed daily, closely-spaced
samples, which stopped being true once ``docs-observability-snapshot.yml``
went manual-only (#2856). These tests lock in the sparse-cadence fix:

- the window line reports a snapshot *count* distinct from the actual
  calendar span between the first and last snapshot, instead of
  conflating the two as "N day(s)",
- a staleness line names the newest snapshot's date and how many days old
  it is relative to a caller-supplied "now",
- three snapshots spread weeks apart still render a real chart (not a
  near-empty one) and the caption no longer claims a daily cadence,
- an empty corpus still renders without crashing.

The script is import-only at module level so these tests drive its pure
functions directly without spawning a subprocess.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

# ``scripts/observability/`` is not an installed package, so load by path.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "observability" / "render_trends.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("observability_render_trends", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["observability_render_trends"] = module
    spec.loader.exec_module(module)
    return module


render_trends = _load_module()


def _snapshot(open_alerts: float = 0.0) -> dict[str, Any]:
    return {
        "backends": [
            {
                "backend": "code-scanning",
                "status": "ok",
                "metrics": [
                    {"name": "open_alerts", "numeric": open_alerts},
                ],
            }
        ]
    }


def test_empty_corpus_renders_without_crashing() -> None:
    md = render_trends.render_markdown([], days=30)
    assert "No snapshots captured yet" in md
    assert "daily" not in md.lower()


def test_window_line_reports_snapshot_count_distinct_from_calendar_span() -> None:
    """A handful of sparse snapshots must not be mislabeled as that many days.

    Three snapshots spread across ten weeks previously rendered as
    "(3 day(s))", implying three consecutive daily captures. The count and
    the true elapsed span must be stated separately.
    """
    snapshots = [
        (dt.date(2026, 1, 5), _snapshot(1.0)),
        (dt.date(2026, 2, 2), _snapshot(2.0)),
        (dt.date(2026, 3, 16), _snapshot(3.0)),
    ]
    md = render_trends.render_markdown(snapshots, days=30, now=dt.date(2026, 3, 16))

    assert "3 snapshot(s)" in md
    assert "70 day(s)" in md  # 2026-01-05 -> 2026-03-16 is 70 calendar days
    assert "3 day(s)" not in md  # the exact mislabeling this test guards against


def test_staleness_line_names_the_newest_snapshot_and_its_age() -> None:
    snapshots = [(dt.date(2026, 7, 16), _snapshot())]
    md = render_trends.render_markdown(snapshots, days=30, now=dt.date(2026, 8, 16))

    assert "2026-07-16" in md
    assert "31 day(s) old" in md


def test_staleness_line_for_a_fresh_snapshot_is_zero_days_old() -> None:
    snapshots = [(dt.date(2026, 8, 16), _snapshot())]
    md = render_trends.render_markdown(snapshots, days=30, now=dt.date(2026, 8, 16))

    assert "0 day(s) old" in md


def test_sparse_snapshots_weeks_apart_still_render_a_real_chart() -> None:
    """The sparse case #3954 asked to be covered explicitly."""
    snapshots = [
        (dt.date(2026, 1, 1), _snapshot(2.0)),
        (dt.date(2026, 1, 20), _snapshot(5.0)),
        (dt.date(2026, 2, 15), _snapshot(1.0)),
    ]
    md = render_trends.render_markdown(snapshots, days=30, now=dt.date(2026, 2, 15))

    # Not the "no numeric data for this backend" fallback - the metric this
    # test populated actually has ticks, even though the gaps between its
    # three snapshots span weeks.
    assert "no numeric data for this backend" not in md
    assert "| open_alerts | `" in md


def test_caption_does_not_claim_a_daily_cadence() -> None:
    snapshots = [
        (dt.date(2026, 1, 1), _snapshot()),
        (dt.date(2026, 3, 1), _snapshot()),
    ]
    md = render_trends.render_markdown(snapshots, days=30, now=dt.date(2026, 3, 1))

    assert "daily" not in md.lower()
    assert "captured snapshot" in md


def test_now_defaults_to_today_when_not_supplied() -> None:
    """Production call sites omit ``now``; it must not be a required arg."""
    today = dt.datetime.now(dt.UTC).date()
    snapshots = [(today, _snapshot())]

    md = render_trends.render_markdown(snapshots, days=30)

    assert "0 day(s) old" in md
