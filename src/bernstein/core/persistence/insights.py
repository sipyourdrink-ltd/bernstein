"""Insights persistence: store and load analytics insights."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from bernstein.core.persistence.atomic_write import write_atomic_json

from .runs_report import RunWrapUp
from .work_ledger import (
    KIND_RUN_CLOSED,
    LedgerReader,
    default_ledger_root,
    run_ledger_dir,
)

_INSIGHTS_FILE = Path(".sdd") / "runtime" / "insights.json"


@dataclass
class InsightsData:
    """Persisted insights data.

    Args:
        timestamp: Unix timestamp when these insights were generated.
        data: Arbitrary insights data (e.g., analytics, patterns, recommendations).
    """

    timestamp: float
    data: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> InsightsData:
        """Deserialise from a JSON-parsed dict.

        Args:
            data: Dict with at least a ``timestamp`` key.

        Returns:
            Populated :class:`InsightsData`.

        Raises:
            KeyError: If ``timestamp`` is absent.
            ValueError: If ``timestamp`` cannot be cast to float.
        """
        return cls(
            timestamp=float(cast(int, data["timestamp"])),
            data=dict(cast("dict[str, Any]", data.get("data", {}))),
        )


def _compute_failure_classes(workdir: Path) -> list[dict[str, Any]]:
    """Compute failure classes insight: gate and check failures grouped by cause.

    Returns a list of dicts, each with keys:
        gate_name: str
        failing_check: str
        count: int
        first_seen: float  (timestamp)
        last_seen: float
    """
    root = default_ledger_root(workdir)
    # Map from (gate_name, failing_check) to {count, first_seen, last_seen}
    classes: dict[tuple[str, str], dict[str, Any]] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        ledger_dir = run_ledger_dir(workdir, child.name)
        reader = LedgerReader(ledger_dir)
        if not reader.exists():
            continue
        entries = list(reader.entries())
        if not entries:
            continue
        # Find the last run.closed entry
        wrapup: RunWrapUp | None = None
        ts: float = 0.0
        for entry in reversed(entries):
            if entry.kind == KIND_RUN_CLOSED:
                wrapup = RunWrapUp.from_payload(entry.payload)
                ts = entry.ts
                break
        if wrapup is None:
            continue
        if not wrapup.gate_name:
            # Not a gate failure
            continue
        gate_name = wrapup.gate_name
        failing_check = wrapup.failing_check or "(failing check not recorded)"
        key = (gate_name, failing_check)
        if key not in classes:
            classes[key] = {"count": 0, "first_seen": ts, "last_seen": ts}
        else:
            classes[key]["last_seen"] = ts
        classes[key]["count"] += 1

    # Convert to list of dicts
    result: list[dict[str, Any]] = []
    for (gate_name, failing_check), data in classes.items():
        result.append(
            {
                "gate_name": gate_name,
                "failing_check": failing_check,
                "count": data["count"],
                "first_seen": data["first_seen"],
                "last_seen": data["last_seen"],
            }
        )
    # Sort by count descending for readability
    result.sort(key=lambda x: x["count"], reverse=True)
    return result


def generate_failure_classes_insights(workdir: Path) -> InsightsData:
    """Generate insights data for failure classes.

    Args:
        workdir: Project root directory.

    Returns:
        InsightsData containing the failure classes insight.
    """
    failure_classes = _compute_failure_classes(workdir)
    return InsightsData(
        timestamp=time.time(),
        data={"failure_classes": failure_classes},
    )


def save_failure_classes_insights(workdir: Path) -> None:
    """Compute and save failure classes insights."""
    data = generate_failure_classes_insights(workdir)
    save_insights(workdir, data)


def save_insights(workdir: Path, data: InsightsData) -> None:
    """Write insights data to ``.sdd/runtime/insights.json``.

    Creates parent directories as needed.  Overwrites any existing file.

    Args:
        workdir: Project root directory.
        data: Insights data to persist.
    """
    insights_path = workdir / _INSIGHTS_FILE
    write_atomic_json(insights_path, data.to_dict())


def load_insights(workdir: Path) -> InsightsData | None:
    """Load insights data from disk, returning None if missing or corrupt.

    Args:
        workdir: Project root directory.

    Returns:
        :class:`InsightsData` if a valid insights file exists; else None.
    """
    insights_path = workdir / _INSIGHTS_FILE
    if not insights_path.exists():
        return None
    try:
        data = json.loads(insights_path.read_text())
        return InsightsData.from_dict(data)
    except (OSError, KeyError, ValueError):
        return None


def delete_insights(workdir: Path) -> None:
    """Remove the insights file so the next load returns None.

    Args:
        workdir: Project root directory.
    """
    insights_path = workdir / _INSIGHTS_FILE
    insights_path.unlink(missing_ok=True)
