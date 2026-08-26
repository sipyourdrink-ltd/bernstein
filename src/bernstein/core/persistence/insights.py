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
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
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


def _compute_flaky_tests(workdir: Path) -> list[dict[str, Any]]:
    """Compute flaky tests insight: tests that failed then passed with no intervening source change.

    Returns a list of dicts, each with keys:
        test_name: str
        flaky_count: int
        first_seen: float  (timestamp)
        last_seen: float
        patterns: list[str]  # sequence of outcomes (e.g., ["failed", "passed"])
    """
    root = default_ledger_root(workdir)
    # Map from test_name to list of (timestamp, outcome) for task.completed/failed entries
    test_sequences: dict[str, list[tuple[float, str]]] = {}

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

        # Process task.completed and task.failed entries in chronological order
        for entry in entries:
            if entry.kind in (KIND_TASK_COMPLETED, KIND_TASK_FAILED):
                # Extract test name from task_id (assuming format like "test_*" or similar)
                task_id = entry.task_id
                if task_id.startswith("test_"):
                    outcome = "passed" if entry.kind == KIND_TASK_COMPLETED else "failed"
                    if task_id not in test_sequences:
                        test_sequences[task_id] = []
                    test_sequences[task_id].append((entry.ts, outcome))

    # Analyze sequences for flaky patterns (failed then passed with no intervening source change)
    # For simplicity, we'll detect any sequence that has both failed and passed states
    flaky_tests: list[dict[str, Any]] = []
    for test_name, sequence in test_sequences.items():
        if len(sequence) < 2:
            continue

        # Sort by timestamp
        sequence.sort(key=lambda x: x[0])

        # Check if we have both failed and passed states
        outcomes = [outcome for _, outcome in sequence]
        if "failed" in outcomes and "passed" in outcomes:
            # Count transitions from failed to passed or passed to failed
            flaky_count = 0
            for i in range(1, len(outcomes)):
                if outcomes[i] != outcomes[i - 1]:
                    flaky_count += 1

            flaky_tests.append(
                {
                    "test_name": test_name,
                    "flaky_count": flaky_count,
                    "first_seen": sequence[0][0],
                    "last_seen": sequence[-1][0],
                    "patterns": [outcome for _, outcome in sequence],
                }
            )

    # Sort by flaky count descending
    flaky_tests.sort(key=lambda x: x["flaky_count"], reverse=True)
    return flaky_tests


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


def generate_flaky_tests_insights(workdir: Path) -> InsightsData:
    """Generate insights data for flaky tests.

    Args:
        workdir: Project root directory.

    Returns:
        InsightsData containing the flaky tests insight.
    """
    flaky_tests = _compute_flaky_tests(workdir)
    return InsightsData(
        timestamp=time.time(),
        data={"flaky_tests": flaky_tests},
    )


def save_failure_classes_insights(workdir: Path) -> None:
    """Compute and save failure classes insights."""
    data = generate_failure_classes_insights(workdir)
    save_insights(workdir, data)


def save_flaky_tests_insights(workdir: Path) -> None:
    """Compute and save flaky tests insights."""
    data = generate_flaky_tests_insights(workdir)
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
