"""Fleet-wide cost rollup.

The per-project cost tracker already maintains a 7-day cumulative number
under ``.sdd/metrics/cost_history.jsonl``. The fleet view aggregates those
values into a single fleet total and renders a sparkline per project plus
one for the fleet.

The sparkline rendering uses Unicode block characters; callers can replace
the helper if they need a different style (e.g. ASCII for terminals that
choke on the eighths-block range).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# U+2581..U+2588 — eighths-block sparkline glyphs.
_SPARKLINE_GLYPHS: tuple[str, ...] = (" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")


@dataclass(slots=True)
class CostSparkline:
    """A renderable sparkline plus its underlying numeric series.

    Attributes:
        series: Source values (oldest first). Always length-bounded.
        glyphs: Pre-rendered Unicode glyphs aligned 1:1 with ``series``.
        peak: Maximum value across the series (used to size the bar).
    """

    series: list[float] = field(default_factory=list[float])
    glyphs: str = ""
    peak: float = 0.0


def render_sparkline(series: list[float]) -> CostSparkline:
    """Render a Unicode sparkline for ``series``.

    Args:
        series: Numeric values; non-negative is assumed but not enforced.

    Returns:
        A :class:`CostSparkline` whose ``glyphs`` attribute is safe to
        write into a Textual widget or HTML page.
    """
    if not series:
        return CostSparkline()
    peak = max(series)
    if peak <= 0:
        return CostSparkline(series=list(series), glyphs=" " * len(series), peak=0.0)
    last_idx = len(_SPARKLINE_GLYPHS) - 1
    glyphs = "".join(_SPARKLINE_GLYPHS[min(last_idx, max(0, round((v / peak) * last_idx)))] for v in series)
    return CostSparkline(series=list(series), glyphs=glyphs, peak=peak)


@dataclass(slots=True)
class FleetCostRollup:
    """Aggregate cost view across the fleet.

    Attributes:
        per_project: Mapping ``project_name -> {total_usd, history, sparkline}``.
        fleet_total_usd: Sum of per-project ``total_usd`` over the window.
        window_days: Width of the rolling window.
    """

    per_project: dict[str, dict[str, object]] = field(default_factory=dict)
    fleet_total_usd: float = 0.0
    window_days: int = 7


def _read_cost_history(sdd_dir: Path, window_days: int) -> list[float]:
    """Read the per-day rolling cost from a project's metrics dir.

    The function tolerates a missing or malformed JSONL file by returning
    an empty series — the dashboard treats that as "no data yet".

    Schema understood:
        - ``{"ts": <epoch>, "cost_usd": <float>}`` per line, OR
        - ``{"date": "YYYY-MM-DD", "cost_usd": <float>}`` per line.
    """
    metrics_path = sdd_dir / "metrics" / "cost_history.jsonl"
    if not metrics_path.exists():
        return []
    cutoff = time.time() - window_days * 86400.0
    bucket: dict[str, float] = {}
    try:
        for raw_line in metrics_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            cost = entry.get("cost_usd") or entry.get("total_usd") or 0.0
            try:
                cost_f = float(cost)
            except (TypeError, ValueError):
                continue
            if "date" in entry and isinstance(entry["date"], str):
                key = entry["date"]
            elif "ts" in entry:
                try:
                    ts = float(entry["ts"])
                except (TypeError, ValueError):
                    continue
                if ts < cutoff:
                    continue
                key = time.strftime("%Y-%m-%d", time.gmtime(ts))
            else:
                continue
            bucket[key] = bucket.get(key, 0.0) + cost_f
    except OSError:
        return []
    return [bucket[k] for k in sorted(bucket)][-window_days:]


def rollup_costs(project_paths: dict[str, Path], *, window_days: int = 7) -> FleetCostRollup:
    """Aggregate per-project rolling costs into a fleet view.

    Args:
        project_paths: Mapping of project name to its ``.sdd`` directory.
        window_days: Rolling window in days.

    Returns:
        A :class:`FleetCostRollup` with sparkline-ready data.
    """
    rollup = FleetCostRollup(window_days=window_days)
    for name, sdd_dir in project_paths.items():
        history = _read_cost_history(sdd_dir, window_days)
        spark = render_sparkline(history)
        total = sum(history)
        rollup.per_project[name] = {
            "total_usd": round(total, 4),
            "history": history,
            "sparkline": spark.glyphs,
            "peak_usd": round(spark.peak, 4),
        }
        rollup.fleet_total_usd += total
    rollup.fleet_total_usd = round(rollup.fleet_total_usd, 4)
    return rollup
