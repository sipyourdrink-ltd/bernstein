"""Cost history persistence and alert logic.

Persists daily cost snapshots to ``.sdd/metrics/cost_history.jsonl``,
maintains a 6-month trailing window, and generates alerts when spend
reaches 80% of the configured budget.

This module is append-only and read-mostly — it never mutates existing
records.  Each record represents one day's aggregated spend.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Keep 6 months of daily snapshots.
_HISTORY_DAYS: int = 180
# Alert threshold: 80% of daily budget.
_ALERT_THRESHOLD: float = 0.80
# Minimum days of history required to compute a meaningful trend.
_MIN_TREND_DAYS: int = 7

_HISTORY_FILE = "cost_history.jsonl"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DailyCostSnapshot:
    """Aggregated cost snapshot for a single calendar day.

    Attributes:
        date_str: ISO-8601 date (``YYYY-MM-DD``).
        spent_usd: Total USD spent across all runs on this day.
        budget_usd: Configured daily budget (0 = unlimited).
        run_count: Number of orchestrator runs that contributed spend.
        timestamp: Unix timestamp when the snapshot was written.
    """

    date_str: str
    spent_usd: float
    budget_usd: float
    run_count: int
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DailyCostSnapshot:
        return cls(
            date_str=str(d["date_str"]),
            spent_usd=float(d["spent_usd"]),
            budget_usd=float(d.get("budget_usd", 0.0)),
            run_count=int(d.get("run_count", 1)),
            timestamp=float(d.get("timestamp", 0.0)),
        )


@dataclass(frozen=True)
class CostTrend:
    """30-day and 90-day trailing cost averages.

    Attributes:
        avg_30d_usd: Average daily spend over the last 30 days.
        avg_90d_usd: Average daily spend over the last 90 days.
        trend_direction: ``"up"``, ``"down"``, or ``"stable"`` compared to the
            prior 30-day window.
        pct_change_30d: Percentage change vs the previous 30-day window.
    """

    avg_30d_usd: float
    avg_90d_usd: float
    trend_direction: str
    pct_change_30d: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "avg_30d_usd": round(self.avg_30d_usd, 6),
            "avg_90d_usd": round(self.avg_90d_usd, 6),
            "trend_direction": self.trend_direction,
            "pct_change_30d": round(self.pct_change_30d, 4),
        }


@dataclass(frozen=True)
class CostAlert:
    """A single cost alert.

    Attributes:
        alert_type: ``"budget_80pct"`` or ``"budget_95pct"``.
        date_str: The date the alert applies to.
        spent_usd: Spend that triggered the alert.
        budget_usd: The budget against which spend was measured.
        percentage_used: Fraction of budget used (0.0-1.0+).
        message: Human-readable description.
    """

    alert_type: str
    date_str: str
    spent_usd: float
    budget_usd: float
    percentage_used: float
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_type": self.alert_type,
            "date_str": self.date_str,
            "spent_usd": round(self.spent_usd, 6),
            "budget_usd": round(self.budget_usd, 6),
            "percentage_used": round(self.percentage_used, 4),
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _history_path(sdd_dir: Path) -> Path:
    metrics_dir = sdd_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    return metrics_dir / _HISTORY_FILE


def append_daily_snapshot(
    sdd_dir: Path,
    spent_usd: float,
    budget_usd: float = 0.0,
    run_count: int = 1,
    snapshot_date: date | None = None,
) -> DailyCostSnapshot:
    """Append a daily cost snapshot to the history file.

    If a snapshot for *snapshot_date* already exists it is **not** replaced
    — callers should aggregate before calling this function.  To update an
    existing day, use :func:`upsert_daily_snapshot` instead.

    Args:
        sdd_dir: The ``.sdd`` directory.
        spent_usd: Total USD spent on this day.
        budget_usd: Configured daily budget (0 = unlimited).
        run_count: Number of runs contributing to this day's spend.
        snapshot_date: Date to record; defaults to today (UTC).

    Returns:
        The newly written :class:`DailyCostSnapshot`.
    """
    if snapshot_date is None:
        snapshot_date = date.today()

    snap = DailyCostSnapshot(
        date_str=snapshot_date.isoformat(),
        spent_usd=round(spent_usd, 6),
        budget_usd=round(budget_usd, 6),
        run_count=run_count,
        timestamp=time.time(),
    )
    path = _history_path(sdd_dir)
    with path.open("a") as fh:
        fh.write(json.dumps(snap.to_dict()) + "\n")

    logger.debug("Appended daily cost snapshot: %s $%.4f", snap.date_str, snap.spent_usd)
    return snap


def upsert_daily_snapshot(
    sdd_dir: Path,
    spent_usd: float,
    budget_usd: float = 0.0,
    run_count: int = 1,
    snapshot_date: date | None = None,
) -> DailyCostSnapshot:
    """Update today's snapshot in-place (last-write-wins by date).

    Loads the full history, replaces any existing entry for *snapshot_date*,
    rewrites the file, and prunes entries older than 6 months.

    Args:
        sdd_dir: The ``.sdd`` directory.
        spent_usd: Total USD spent on this day (full day total, not delta).
        budget_usd: Configured daily budget (0 = unlimited).
        run_count: Number of runs contributing to this day's spend.
        snapshot_date: Date to record; defaults to today (UTC).

    Returns:
        The upserted :class:`DailyCostSnapshot`.
    """
    if snapshot_date is None:
        snapshot_date = date.today()

    snap = DailyCostSnapshot(
        date_str=snapshot_date.isoformat(),
        spent_usd=round(spent_usd, 6),
        budget_usd=round(budget_usd, 6),
        run_count=run_count,
        timestamp=time.time(),
    )

    existing = load_history(sdd_dir)
    # Replace any same-date entry; keep the rest within the 6-month window.
    cutoff = date.today() - timedelta(days=_HISTORY_DAYS)
    updated: list[DailyCostSnapshot] = [
        s for s in existing if s.date_str != snap.date_str and date.fromisoformat(s.date_str) >= cutoff
    ]
    updated.append(snap)
    updated.sort(key=lambda s: s.date_str)

    path = _history_path(sdd_dir)
    with path.open("w") as fh:
        for s in updated:
            fh.write(json.dumps(s.to_dict()) + "\n")

    return snap


def load_history(sdd_dir: Path, days: int = _HISTORY_DAYS) -> list[DailyCostSnapshot]:
    """Load cost history snapshots from disk.

    Args:
        sdd_dir: The ``.sdd`` directory.
        days: Maximum age of snapshots to return (default: 180).

    Returns:
        List of :class:`DailyCostSnapshot` sorted oldest-first, limited to
        the last *days* calendar days.
    """
    path = _history_path(sdd_dir)
    if not path.exists():
        return []

    cutoff = date.today() - timedelta(days=days)
    snapshots: list[DailyCostSnapshot] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                snap = DailyCostSnapshot.from_dict(d)
                if date.fromisoformat(snap.date_str) >= cutoff:
                    snapshots.append(snap)
            except Exception as exc:
                logger.warning("Skipping malformed cost_history line: %s — %s", line[:80], exc)
    except OSError as exc:
        logger.warning("Could not read cost history: %s", exc)

    snapshots.sort(key=lambda s: s.date_str)
    return snapshots


# ---------------------------------------------------------------------------
# Trend computation
# ---------------------------------------------------------------------------


def compute_trends(snapshots: list[DailyCostSnapshot]) -> CostTrend:
    """Compute 30-day and 90-day trailing cost averages and trend direction.

    Args:
        snapshots: Sorted (oldest-first) list of snapshots.

    Returns:
        :class:`CostTrend` with averages and direction.  When insufficient
        data is available, averages default to the mean of what exists and
        ``trend_direction`` is ``"stable"``.
    """
    today = date.today()

    def _avg(days: int) -> float:
        cutoff = today - timedelta(days=days)
        window = [s.spent_usd for s in snapshots if date.fromisoformat(s.date_str) >= cutoff]
        return sum(window) / len(window) if window else 0.0

    avg_30 = _avg(30)
    avg_90 = _avg(90)

    # Compare current 30-day window vs previous 30-day window (days 31-60)
    prev_cutoff_end = today - timedelta(days=30)
    prev_cutoff_start = today - timedelta(days=60)
    prev_window = [
        s.spent_usd for s in snapshots if prev_cutoff_start <= date.fromisoformat(s.date_str) < prev_cutoff_end
    ]
    prev_avg = sum(prev_window) / len(prev_window) if prev_window else avg_30

    if prev_avg > 0 and len(prev_window) >= _MIN_TREND_DAYS:
        pct_change = (avg_30 - prev_avg) / prev_avg
        if pct_change > 0.05:
            direction = "up"
        elif pct_change < -0.05:
            direction = "down"
        else:
            direction = "stable"
    else:
        pct_change = 0.0
        direction = "stable"

    return CostTrend(
        avg_30d_usd=round(avg_30, 6),
        avg_90d_usd=round(avg_90, 6),
        trend_direction=direction,
        pct_change_30d=round(pct_change, 4),
    )


# ---------------------------------------------------------------------------
# Alert generation
# ---------------------------------------------------------------------------


def get_active_alerts(
    sdd_dir: Path,
    current_spent_usd: float,
    budget_usd: float,
) -> list[CostAlert]:
    """Return active budget alerts given the current run's spend.

    Alerts are generated when *current_spent_usd* reaches 80% or 95% of
    *budget_usd*.  When *budget_usd* is 0 (unlimited), no alerts are emitted.

    Args:
        sdd_dir: The ``.sdd`` directory (used to read daily history).
        current_spent_usd: Total spend for the current run / today.
        budget_usd: Daily / run budget in USD (0 = unlimited).

    Returns:
        List of :class:`CostAlert` objects (may be empty).
    """
    if budget_usd <= 0:
        return []

    alerts: list[CostAlert] = []
    today_str = date.today().isoformat()
    pct = current_spent_usd / budget_usd

    if pct >= 0.95:
        alerts.append(
            CostAlert(
                alert_type="budget_95pct",
                date_str=today_str,
                spent_usd=round(current_spent_usd, 6),
                budget_usd=round(budget_usd, 6),
                percentage_used=round(pct, 4),
                message=(
                    f"CRITICAL: ${current_spent_usd:.2f} of ${budget_usd:.2f} budget used "
                    f"({pct * 100:.1f}%) — agent spawns will stop at 100%"
                ),
            )
        )
    elif pct >= _ALERT_THRESHOLD:
        alerts.append(
            CostAlert(
                alert_type="budget_80pct",
                date_str=today_str,
                spent_usd=round(current_spent_usd, 6),
                budget_usd=round(budget_usd, 6),
                percentage_used=round(pct, 4),
                message=(f"WARNING: ${current_spent_usd:.2f} of ${budget_usd:.2f} budget used ({pct * 100:.1f}%)"),
            )
        )

    return alerts
