"""Tests for the fleet cost rollup."""

from __future__ import annotations

import json
import time
from pathlib import Path

from bernstein.core.fleet.cost_rollup import (
    render_sparkline,
    rollup_costs,
)


def _write_history(sdd_dir: Path, samples: list[tuple[str, float]]) -> None:
    metrics = sdd_dir / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    with (metrics / "cost_history.jsonl").open("w", encoding="utf-8") as fh:
        for date, cost in samples:
            fh.write(json.dumps({"date": date, "cost_usd": cost}) + "\n")


def test_render_sparkline_empty() -> None:
    spark = render_sparkline([])
    assert spark.glyphs == ""
    assert spark.peak == 0.0


def test_render_sparkline_monotonic() -> None:
    spark = render_sparkline([1.0, 2.0, 4.0, 8.0])
    assert len(spark.glyphs) == 4
    assert spark.peak == 8.0


def test_rollup_correctness(tmp_path: Path) -> None:
    """Per-project totals and fleet total agree with the underlying samples."""
    a = tmp_path / "alpha" / ".sdd"
    b = tmp_path / "bravo" / ".sdd"
    _write_history(
        a,
        [("2026-04-19", 1.0), ("2026-04-20", 2.0), ("2026-04-21", 3.0)],
    )
    _write_history(
        b,
        [("2026-04-20", 4.0), ("2026-04-21", 5.0)],
    )
    rollup = rollup_costs({"alpha": a, "bravo": b}, window_days=7)
    assert rollup.per_project["alpha"]["total_usd"] == 6.0
    assert rollup.per_project["bravo"]["total_usd"] == 9.0
    assert rollup.fleet_total_usd == 15.0
    assert isinstance(rollup.per_project["alpha"]["sparkline"], str)
    assert len(rollup.per_project["alpha"]["sparkline"]) == 3


def test_rollup_handles_missing_project(tmp_path: Path) -> None:
    """Projects without history files yield empty sparklines but no crash."""
    rollup = rollup_costs({"empty": tmp_path / "missing" / ".sdd"})
    assert rollup.per_project["empty"]["total_usd"] == 0.0
    assert rollup.per_project["empty"]["sparkline"] == ""
    assert rollup.fleet_total_usd == 0.0


def test_rollup_window_truncates(tmp_path: Path) -> None:
    """Only the last ``window_days`` daily samples are kept."""
    sdd = tmp_path / "alpha" / ".sdd"
    samples = [
        ("2026-04-15", 100.0),  # outside default 7-day window slot
        ("2026-04-16", 1.0),
        ("2026-04-17", 1.0),
        ("2026-04-18", 1.0),
        ("2026-04-19", 1.0),
        ("2026-04-20", 1.0),
        ("2026-04-21", 1.0),
        ("2026-04-22", 1.0),
    ]
    _write_history(sdd, samples)
    rollup = rollup_costs({"alpha": sdd}, window_days=7)
    history = rollup.per_project["alpha"]["history"]
    assert isinstance(history, list)
    assert len(history) == 7  # 7-day cap


def test_rollup_uses_ts_field(tmp_path: Path) -> None:
    """Entries with epoch ``ts`` instead of ``date`` are still aggregated."""
    sdd = tmp_path / "alpha" / ".sdd"
    metrics = sdd / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with (metrics / "cost_history.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now - 1, "cost_usd": 2.0}) + "\n")
        fh.write(json.dumps({"ts": now, "cost_usd": 3.0}) + "\n")
    rollup = rollup_costs({"alpha": sdd})
    assert rollup.per_project["alpha"]["total_usd"] == 5.0
