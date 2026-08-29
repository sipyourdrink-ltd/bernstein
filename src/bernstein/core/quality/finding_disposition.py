"""Disposition ledger for review findings.

Tracks how each finding was resolved: whether it was confirmed, dismissed as
a false positive, accepted as a known limitation, or fixed.  Provides
durable storage and reconciliation so the disposition record is auditable
across runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


class FindingDisposition(StrEnum):
    """Terminal disposition for a review finding.

    Attributes:
        OPEN: Not yet evaluated; awaiting triage.
        CONFIRMED: Valid finding, should be addressed.
        FALSE_POSITIVE: Incorrectly flagged; dismissed.
        ACCEPTED_RISK: Valid but accepted as known limitation.
        FIXED: Resolved by a subsequent change.
        DUPLICATE: Redundant with another finding; suppressed.
    """

    OPEN = "open"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    ACCEPTED_RISK = "accepted_risk"
    FIXED = "fixed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class FindingDispositionRecord:
    """One finding's disposition entry in the ledger.

    Attributes:
        finding_id: Stable identifier for the finding (derived from file, line,
            and category hash).
        disposition: The applied disposition.
        resolved_by: Agent or operator who set the disposition.
        rationale: Human-readable reason for the disposition.
        task_id: Associated task id that introduced or addressed the finding.
        timestamp: Unix timestamp when the disposition was recorded.
    """

    finding_id: str
    disposition: FindingDisposition
    resolved_by: str
    rationale: str = ""
    task_id: str = ""
    timestamp: int = field(default_factory=lambda: int(datetime.now(UTC).timestamp()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "disposition": self.disposition.value,
            "resolved_by": self.resolved_by,
            "rationale": self.rationale,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FindingDispositionRecord:
        return cls(
            finding_id=str(data["finding_id"]),
            disposition=FindingDisposition(data["disposition"]),
            resolved_by=str(data["resolved_by"]),
            rationale=str(data.get("rationale", "")),
            task_id=str(data.get("task_id", "")),
            timestamp=int(data.get("timestamp", 0)),
        )


#: Subpath under the run directory where the disposition ledger is stored.
_DISPOSITION_LEDGER_FILENAME = "finding_dispositions.jsonl"


def disposition_ledger_path(runtime_dir: Path) -> Path:
    """Return the path to the finding disposition ledger for *runtime_dir*."""
    return runtime_dir / _DISPOSITION_LEDGER_FILENAME


def record_disposition(
    *,
    finding_id: str,
    disposition: FindingDisposition,
    resolved_by: str,
    rationale: str = "",
    task_id: str = "",
    runtime_dir: Path,
) -> FindingDispositionRecord:
    """Record a finding's disposition to the durable ledger.

    Args:
        finding_id: Stable finding identifier.
        disposition: The applied disposition.
        resolved_by: Agent or operator setting the disposition.
        rationale: Human-readable reason.
        task_id: Associated task id.
        runtime_dir: The run's ``.sdd/runtime`` directory.

    Returns:
        The persisted :class:`FindingDispositionRecord`.
    """
    record = FindingDispositionRecord(
        finding_id=finding_id,
        disposition=disposition,
        resolved_by=resolved_by,
        rationale=rationale,
        task_id=task_id,
    )
    ledger_path = disposition_ledger_path(runtime_dir)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
    return record


def load_disposition_ledger(runtime_dir: Path) -> list[FindingDispositionRecord]:
    """Load all disposition records from the ledger.

    Args:
        runtime_dir: The run's ``.sdd/runtime`` directory.

    Returns:
        All disposition records in append order.
    """
    ledger_path = disposition_ledger_path(runtime_dir)
    if not ledger_path.is_file():
        return []
    records: list[FindingDispositionRecord] = []
    with ledger_path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(FindingDispositionRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                import logging

                logging.getLogger(__name__).warning(
                    "disposition_ledger: skipping malformed line %d in %s: %s",
                    line_no,
                    ledger_path,
                    exc,
                )
                continue
    return records


def get_current_disposition(
    finding_id: str,
    runtime_dir: Path,
) -> FindingDisposition | None:
    """Return the most recent disposition for *finding_id*, or None if not found.

    The ledger is append-only; later entries supersede earlier ones for the
    same finding_id.
    """
    records = load_disposition_ledger(runtime_dir)
    for record in reversed(records):
        if record.finding_id == finding_id:
            return record.disposition
    return None


__all__ = [
    "FindingDisposition",
    "FindingDispositionRecord",
    "disposition_ledger_path",
    "get_current_disposition",
    "load_disposition_ledger",
    "record_disposition",
]
