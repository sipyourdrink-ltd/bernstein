"""Agent performance leaderboard (ROAD-036).

Aggregate task history by (adapter, model, task_type) to rank agent
configurations by success rate, cost, and duration.  Provides
recommendations for the best adapter/model per task type.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaderboardRecord:
    """Aggregated performance for one (adapter, model, task_type) triple.

    Attributes:
        adapter: CLI agent adapter name (e.g. "claude", "codex").
        model: Model identifier used by the adapter.
        task_type: Task type or role (e.g. "backend", "qa").
        attempts: Total tasks attempted.
        successes: Tasks completed successfully.
        avg_cost_usd: Mean cost per task in USD.
        avg_duration_s: Mean wall-clock duration per task in seconds.
        quality_rate: Fraction of tasks passing quality gates (0.0-1.0).
    """

    adapter: str
    model: str
    task_type: str
    attempts: int
    successes: int
    avg_cost_usd: float
    avg_duration_s: float
    quality_rate: float

    @property
    def success_rate(self) -> float:
        """Task success rate (0.0-1.0)."""
        if self.attempts == 0:
            return 0.0
        return self.successes / self.attempts


@dataclass(frozen=True)
class Leaderboard:
    """Snapshot of agent performance rankings.

    Attributes:
        records: Aggregated performance records.
        generated_at: ISO-8601 timestamp of generation.
    """

    records: list[LeaderboardRecord]
    generated_at: str


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def build_leaderboard(history: list[dict[str, Any]]) -> Leaderboard:
    """Aggregate task history into a leaderboard.

    Each dict in *history* should contain at minimum:
      - ``adapter`` (str)
      - ``model`` (str)
      - ``task_type`` or ``role`` (str)
      - ``success`` (bool)

    Optional fields: ``cost_usd`` (float), ``duration_s`` (float),
    ``quality_pass`` (bool).

    Args:
        history: List of task-result dicts.

    Returns:
        A :class:`Leaderboard` with one record per unique
        (adapter, model, task_type) group.
    """
    groups: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "attempts": 0,
            "successes": 0,
            "total_cost": 0.0,
            "total_duration": 0.0,
            "quality_passes": 0,
        }
    )

    for entry in history:
        adapter = entry.get("adapter", "unknown")
        model = entry.get("model", "unknown")
        task_type = entry.get("task_type") or entry.get("role", "unknown")
        key = (adapter, model, task_type)

        grp = groups[key]
        grp["attempts"] += 1
        if entry.get("success", False):
            grp["successes"] += 1
        grp["total_cost"] += float(entry.get("cost_usd", 0.0) or 0.0)
        grp["total_duration"] += float(entry.get("duration_s", 0.0) or 0.0)
        if entry.get("quality_pass", False):
            grp["quality_passes"] += 1

    records: list[LeaderboardRecord] = []
    for (adapter, model, task_type), grp in sorted(groups.items()):
        attempts = grp["attempts"]
        records.append(
            LeaderboardRecord(
                adapter=adapter,
                model=model,
                task_type=task_type,
                attempts=attempts,
                successes=grp["successes"],
                avg_cost_usd=grp["total_cost"] / attempts if attempts > 0 else 0.0,
                avg_duration_s=grp["total_duration"] / attempts if attempts > 0 else 0.0,
                quality_rate=grp["quality_passes"] / attempts if attempts > 0 else 0.0,
            )
        )

    return Leaderboard(
        records=records,
        generated_at=datetime.now(tz=UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_SORT_KEYS: dict[str, Any] = {
    "success_rate": lambda r: r.success_rate,
    "cost": lambda r: r.avg_cost_usd,
    "duration": lambda r: r.avg_duration_s,
    "quality": lambda r: r.quality_rate,
    "attempts": lambda r: r.attempts,
}

# Sort directions: True = descending (higher is better), False = ascending
_SORT_DESCENDING: dict[str, bool] = {
    "success_rate": True,
    "cost": False,
    "duration": False,
    "quality": True,
    "attempts": True,
}


def _recommendation_label(record: LeaderboardRecord) -> str:
    """Generate a short recommendation label for a record."""
    labels: list[str] = []
    if record.success_rate >= 0.9:
        labels.append("Reliable")
    if record.avg_cost_usd <= 0.01 and record.attempts > 0:
        labels.append("Low-cost")
    if record.avg_duration_s <= 30.0 and record.attempts > 0:
        labels.append("Fast")
    if record.quality_rate >= 0.9:
        labels.append("High-quality")
    if not labels:
        return ""
    return ", ".join(labels)


def format_leaderboard(lb: Leaderboard, sort_by: str = "success_rate") -> str:
    """Render the leaderboard as a Rich-formatted table string.

    Args:
        lb: Leaderboard to render.
        sort_by: Metric to sort by.  One of ``"success_rate"``,
            ``"cost"``, ``"duration"``, ``"quality"``, ``"attempts"``.

    Returns:
        Rendered table as a string (with ANSI codes stripped).
    """
    from rich.console import Console
    from rich.table import Table

    key_fn = _SORT_KEYS.get(sort_by, _SORT_KEYS["success_rate"])
    descending = _SORT_DESCENDING.get(sort_by, True)
    sorted_records = sorted(lb.records, key=key_fn, reverse=descending)

    table = Table(
        title="Agent Performance Leaderboard",
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("#", justify="right", min_width=3)
    table.add_column("Adapter", min_width=10)
    table.add_column("Model", min_width=12)
    table.add_column("Task Type", min_width=10)
    table.add_column("Attempts", justify="right", min_width=8)
    table.add_column("Success", justify="right", min_width=8)
    table.add_column("Avg Cost", justify="right", min_width=10)
    table.add_column("Avg Duration", justify="right", min_width=12)
    table.add_column("Quality", justify="right", min_width=8)
    table.add_column("Recommendation", min_width=14)

    for rank, record in enumerate(sorted_records, start=1):
        pct = f"{record.success_rate * 100:.0f}%"
        cost = f"${record.avg_cost_usd:.4f}"
        dur = f"{record.avg_duration_s:.1f}s"
        qual = f"{record.quality_rate * 100:.0f}%"
        rec_label = _recommendation_label(record)
        table.add_row(
            str(rank),
            record.adapter,
            record.model,
            record.task_type,
            str(record.attempts),
            pct,
            cost,
            dur,
            qual,
            rec_label,
        )

    console = Console(record=True, width=120)
    console.print(table)
    return console.export_text()


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


def get_recommendation(
    task_type: str,
    lb: Leaderboard,
) -> LeaderboardRecord | None:
    """Return the best adapter/model for a given task type.

    Selection criteria: highest ``success_rate``, with ties broken by
    lowest ``avg_cost_usd``.

    Args:
        task_type: Task type to filter on (e.g. "backend").
        lb: Leaderboard to search.

    Returns:
        Best-matching :class:`LeaderboardRecord`, or ``None`` if no
        records match the task type.
    """
    candidates = [r for r in lb.records if r.task_type == task_type]
    if not candidates:
        return None
    # Sort: highest success_rate first, then lowest cost
    candidates.sort(key=lambda r: (-r.success_rate, r.avg_cost_usd))
    return candidates[0]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_leaderboard(lb: Leaderboard, path: Path) -> None:
    """Serialise a leaderboard to a JSON file.

    Args:
        lb: Leaderboard to save.
        path: Destination file path.
    """
    data = {
        "generated_at": lb.generated_at,
        "records": [asdict(r) for r in lb.records],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def load_leaderboard(path: Path) -> Leaderboard | None:
    """Load a leaderboard from a JSON file.

    Args:
        path: Source file path.

    Returns:
        Parsed :class:`Leaderboard`, or ``None`` if the file does not
        exist or cannot be parsed.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load leaderboard from %s", path)
        return None

    records = [
        LeaderboardRecord(
            adapter=r["adapter"],
            model=r["model"],
            task_type=r["task_type"],
            attempts=r["attempts"],
            successes=r["successes"],
            avg_cost_usd=r["avg_cost_usd"],
            avg_duration_s=r["avg_duration_s"],
            quality_rate=r["quality_rate"],
        )
        for r in data.get("records", [])
    ]
    return Leaderboard(records=records, generated_at=data.get("generated_at", ""))
