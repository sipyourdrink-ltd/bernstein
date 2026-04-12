"""Pivot signal system for strategic re-evaluation of tickets.

Agents file pivot signals when they discover something that changes strategic
direction. Small pivots are handled inline; large pivots route to VP for review.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PivotSignal:
    """A strategic pivot discovered by an agent during task execution.

    Attributes:
        timestamp: ISO-8601 timestamp of when the pivot was discovered.
        agent_id: ID of the agent that filed the signal.
        task_id: ID of the task during which the pivot was discovered.
        signal_type: Type of signal (currently only ``strategic_pivot``).
        severity: Impact level — low/medium pivots are handled inline,
            high pivots route to VP.
        summary: Human-readable description of what was discovered.
        affected_tickets: Task IDs whose assumptions are invalidated.
        proposed_action: What the agent recommends doing about it.
    """

    timestamp: str
    agent_id: str
    task_id: str
    signal_type: Literal["strategic_pivot"] = "strategic_pivot"
    severity: Literal["low", "medium", "high"] = "medium"
    summary: str = ""
    affected_tickets: list[str] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    proposed_action: str = ""


@dataclass(frozen=True)
class TicketChange:
    """Record of a ticket mutation caused by a pivot signal.

    Attributes:
        timestamp: ISO-8601 timestamp of the change.
        pivot_signal_task_id: The task_id from the originating PivotSignal.
        changed_by: Role or agent ID that made the change.
        ticket_id: ID of the ticket that was changed.
        field_name: Which field was modified (e.g. ``priority``, ``description``).
        before: Previous value (serialised as string).
        after: New value (serialised as string).
    """

    timestamp: str
    pivot_signal_task_id: str
    changed_by: str
    ticket_id: str
    field_name: str
    before: str
    after: str


@dataclass
class VPDecision:
    """Result of VP evaluation of a high-severity pivot.

    Attributes:
        pivot_task_id: The task_id from the PivotSignal being evaluated.
        decision: APPROVE (update tickets), REJECT (note and proceed),
            or ESCALATE (pause work, notify human).
        rationale: Why this decision was made.
        ticket_updates: Mapping of ticket_id to dict of field changes.
        timestamp: ISO-8601 timestamp of the decision.
    """

    pivot_task_id: str
    decision: Literal["approve", "reject", "escalate"]
    rationale: str = ""
    ticket_updates: dict[str, dict[str, str]] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )


def _signals_dir(workdir: Path) -> Path:
    """Return the signals directory, creating it if needed."""
    d = workdir / ".sdd" / "signals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_pivot_signal(signal: PivotSignal, workdir: Path) -> None:
    """Append a pivot signal to the JSONL log.

    Args:
        signal: The pivot signal to file.
        workdir: Project root (contains ``.sdd/``).
    """
    path = _signals_dir(workdir) / "pivots.jsonl"
    line = json.dumps(asdict(signal), default=str)
    with path.open("a") as f:
        f.write(line + "\n")
    logger.info(
        "Filed %s pivot signal from agent %s (task %s): %s",
        signal.severity,
        signal.agent_id,
        signal.task_id,
        signal.summary[:80],
    )


def record_ticket_change(change: TicketChange, workdir: Path) -> None:
    """Append a ticket change record to the JSONL log.

    Args:
        change: The ticket change to record.
        workdir: Project root (contains ``.sdd/``).
    """
    path = _signals_dir(workdir) / "ticket_changes.jsonl"
    line = json.dumps(asdict(change), default=str)
    with path.open("a") as f:
        f.write(line + "\n")


def read_pivot_signals(workdir: Path) -> list[PivotSignal]:
    """Read all pivot signals from the JSONL log.

    Args:
        workdir: Project root (contains ``.sdd/``).

    Returns:
        List of PivotSignal objects, oldest first.
    """
    path = _signals_dir(workdir) / "pivots.jsonl"
    if not path.exists():
        return []

    signals: list[PivotSignal] = []
    for line_num, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            signals.append(PivotSignal(**raw))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Skipping malformed pivot signal at line %d: %s", line_num, exc)
    return signals


def read_unresolved_pivots(workdir: Path) -> list[PivotSignal]:
    """Read high-severity pivot signals that have no VP decision yet.

    A pivot is unresolved if its task_id does not appear in any
    VPDecision in ``vp_decisions.jsonl``.

    Args:
        workdir: Project root.

    Returns:
        List of unresolved high-severity PivotSignals.
    """
    all_signals = read_pivot_signals(workdir)
    high_signals = [s for s in all_signals if s.severity == "high"]
    if not high_signals:
        return []

    resolved_task_ids = _read_resolved_pivot_ids(workdir)
    return [s for s in high_signals if s.task_id not in resolved_task_ids]


def _read_resolved_pivot_ids(workdir: Path) -> set[str]:
    """Return task_ids of pivots that have a VP decision on file."""
    path = _signals_dir(workdir) / "vp_decisions.jsonl"
    if not path.exists():
        return set()

    resolved: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            resolved.add(raw["pivot_task_id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return resolved


def record_vp_decision(decision: VPDecision, workdir: Path) -> None:
    """Append a VP decision to the JSONL log.

    Args:
        decision: The VP decision to record.
        workdir: Project root.
    """
    path = _signals_dir(workdir) / "vp_decisions.jsonl"
    line = json.dumps(asdict(decision), default=str)
    with path.open("a") as f:
        f.write(line + "\n")
    logger.info(
        "VP decision for pivot %s: %s — %s",
        decision.pivot_task_id,
        decision.decision,
        decision.rationale[:80],
    )


def needs_vp_review(signal: PivotSignal) -> bool:
    """Determine whether a pivot signal requires VP-level review.

    Large pivots (severity=high, OR affects 3+ tickets) route to VP.
    Small pivots (low/medium, affects 1-2 tickets) are handled inline.

    Args:
        signal: The pivot signal to evaluate.

    Returns:
        True if VP review is required.
    """
    if signal.severity == "high":
        return True
    return len(signal.affected_tickets) >= 3
