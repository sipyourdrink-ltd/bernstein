"""Verification nudge system for unverified task completions.

Tracks when agents complete tasks without running tests or verification steps.
Maintains a JSONL ledger at ``.sdd/metrics/verification_nudges.jsonl`` and
exposes a summary that the TUI dashboard and ``bernstein status`` can display.

Design:
- Each completed task is checked for verification evidence (test runs,
  quality gates, completion signals) via the agent log summary.
- Tasks that complete without any verification are flagged as "unverified".
- When the ratio of unverified completions exceeds a configurable threshold,
  a nudge alert is surfaced in the dashboard.
- The ledger is append-only JSONL for crash-safety and auditability.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Fraction of unverified completions that triggers a nudge alert.
DEFAULT_NUDGE_THRESHOLD: float = 0.3

#: Minimum number of completions before threshold evaluation kicks in.
MIN_COMPLETIONS_FOR_NUDGE: int = 3

#: Filename for the JSONL ledger inside .sdd/metrics/.
_LEDGER_FILENAME: str = "verification_nudges.jsonl"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationRecord:
    """Single task completion verification record.

    Attributes:
        task_id: ID of the completed task.
        session_id: Agent session that completed the task.
        timestamp: Unix epoch when the record was created.
        tests_run: Whether the agent ran tests (from log analysis).
        quality_gates_run: Whether quality gates were executed.
        completion_signals_checked: Whether janitor completion signals were checked.
        verified: True if any verification evidence was found.
    """

    task_id: str
    session_id: str
    timestamp: float
    tests_run: bool
    quality_gates_run: bool
    completion_signals_checked: bool
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "tests_run": self.tests_run,
            "quality_gates_run": self.quality_gates_run,
            "completion_signals_checked": self.completion_signals_checked,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerificationRecord:
        """Deserialize from a JSON dict."""
        return cls(
            task_id=str(data.get("task_id", "")),
            session_id=str(data.get("session_id", "")),
            timestamp=float(data.get("timestamp", 0.0)),
            tests_run=bool(data.get("tests_run", False)),
            quality_gates_run=bool(data.get("quality_gates_run", False)),
            completion_signals_checked=bool(data.get("completion_signals_checked", False)),
            verified=bool(data.get("verified", False)),
        )


@dataclass(frozen=True)
class NudgeSummary:
    """Aggregated verification nudge state for display.

    Attributes:
        total_completions: Total tasks that completed.
        verified_count: Tasks that had at least one verification step.
        unverified_count: Tasks that completed without any verification.
        unverified_ratio: Fraction of unverified completions (0.0-1.0).
        threshold_exceeded: True when unverified_ratio > configured threshold.
        nudge_threshold: The configured threshold value.
        recent_unverified: Task IDs of the most recent unverified completions.
    """

    total_completions: int
    verified_count: int
    unverified_count: int
    unverified_ratio: float
    threshold_exceeded: bool
    nudge_threshold: float
    recent_unverified: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "total_completions": self.total_completions,
            "verified_count": self.verified_count,
            "unverified_count": self.unverified_count,
            "unverified_ratio": round(self.unverified_ratio, 3),
            "threshold_exceeded": self.threshold_exceeded,
            "nudge_threshold": self.nudge_threshold,
            "recent_unverified": self.recent_unverified,
        }


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


@dataclass
class VerificationNudgeTracker:
    """Track and evaluate verification status of task completions.

    Thread-safe: all mutations are protected by a lock. The JSONL ledger
    is flushed on every ``record()`` call so state survives crashes.

    Attributes:
        metrics_dir: Directory for the JSONL ledger (typically .sdd/metrics/).
        nudge_threshold: Unverified-ratio above which alerts fire.
        records: In-memory list of records (loaded from ledger on init).
    """

    metrics_dir: Path
    nudge_threshold: float = DEFAULT_NUDGE_THRESHOLD
    records: list[VerificationRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        """Load existing records from the JSONL ledger on disk."""
        self._load_ledger()

    # -- public API ----------------------------------------------------------

    def record(
        self,
        *,
        task_id: str,
        session_id: str,
        tests_run: bool = False,
        quality_gates_run: bool = False,
        completion_signals_checked: bool = False,
    ) -> VerificationRecord:
        """Record a task completion's verification status.

        Args:
            task_id: ID of the completed task.
            session_id: Agent session that completed it.
            tests_run: Whether the agent ran tests.
            quality_gates_run: Whether quality gates executed.
            completion_signals_checked: Whether janitor checked signals.

        Returns:
            The created VerificationRecord.
        """
        verified = tests_run or quality_gates_run or completion_signals_checked
        rec = VerificationRecord(
            task_id=task_id,
            session_id=session_id,
            timestamp=time.time(),
            tests_run=tests_run,
            quality_gates_run=quality_gates_run,
            completion_signals_checked=completion_signals_checked,
            verified=verified,
        )
        with self._lock:
            self.records.append(rec)
            self._append_to_ledger(rec)
        if not verified:
            logger.info(
                "Unverified completion: task %s (session %s) — no tests, "
                "quality gates, or completion signals detected",
                task_id,
                session_id,
            )
        return rec

    def summary(self) -> NudgeSummary:
        """Compute current verification nudge summary.

        Returns:
            NudgeSummary with counts and threshold evaluation.
        """
        with self._lock:
            total = len(self.records)
            verified = sum(1 for r in self.records if r.verified)
            unverified = total - verified

        ratio = unverified / total if total > 0 else 0.0
        threshold_exceeded = (
            total >= MIN_COMPLETIONS_FOR_NUDGE and ratio > self.nudge_threshold
        )
        # Last 5 unverified task IDs for display
        recent: list[str] = []
        with self._lock:
            for r in reversed(self.records):
                if not r.verified:
                    recent.append(r.task_id)
                if len(recent) >= 5:
                    break

        return NudgeSummary(
            total_completions=total,
            verified_count=verified,
            unverified_count=unverified,
            unverified_ratio=ratio,
            threshold_exceeded=threshold_exceeded,
            nudge_threshold=self.nudge_threshold,
            recent_unverified=recent,
        )

    def is_task_recorded(self, task_id: str) -> bool:
        """Check whether a task has already been recorded.

        Args:
            task_id: The task ID to check.

        Returns:
            True if a record for this task already exists.
        """
        with self._lock:
            return any(r.task_id == task_id for r in self.records)

    def reset(self) -> None:
        """Clear all in-memory records (does NOT delete ledger file)."""
        with self._lock:
            self.records.clear()

    # -- persistence ---------------------------------------------------------

    def _ledger_path(self) -> Path:
        return self.metrics_dir / _LEDGER_FILENAME

    def _load_ledger(self) -> None:
        """Load existing records from the JSONL ledger file."""
        path = self._ledger_path()
        if not path.exists():
            return
        try:
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    self.records.append(VerificationRecord.from_dict(data))
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    logger.debug("Skipping malformed ledger line: %s", exc)
        except OSError as exc:
            logger.warning("Failed to read verification ledger %s: %s", path, exc)

    def _append_to_ledger(self, record: VerificationRecord) -> None:
        """Append a single record to the JSONL ledger."""
        path = self._ledger_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("Failed to write verification ledger: %s", exc)


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def load_nudge_summary(metrics_dir: Path, *, threshold: float = DEFAULT_NUDGE_THRESHOLD) -> NudgeSummary:
    """Load verification nudge summary from disk without keeping tracker alive.

    Useful for one-shot reads from the status command or API routes.

    Args:
        metrics_dir: Path to .sdd/metrics/ directory.
        threshold: Nudge threshold for evaluation.

    Returns:
        NudgeSummary computed from the on-disk ledger.
    """
    tracker = VerificationNudgeTracker(metrics_dir=metrics_dir, nudge_threshold=threshold)
    return tracker.summary()
