"""Activity tracking metrics with duration accounting.

Tracks user vs agent activity time, persisting metrics to
``.sdd/metrics/activity.jsonl``.  All operations are thread-safe.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_ACTIVITY_FILE = "activity.jsonl"


class ActivityCategory(Enum):
    """Categories of tracked activity."""

    PLANNING = "planning"
    CODING = "coding"
    TESTING = "testing"
    REVIEWING = "reviewing"
    WAITING = "waiting"
    OTHER = "other"


@dataclass
class ActivityMetric:
    """A single recorded activity metric.

    Attributes:
        timestamp: Unix timestamp when the activity started.
        category: Activity category (planning, coding, etc.).
        duration_s: Duration of the activity in seconds.
        description: Human-readable description of what was done.
    """

    timestamp: float
    category: str
    duration_s: float
    description: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serialisable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActivityMetric:
        """Reconstruct an ActivityMetric from a dict."""
        return cls(
            timestamp=data["timestamp"],
            category=data["category"],
            duration_s=data["duration_s"],
            description=data["description"],
        )


class ActivitySession:
    """Thread-safe activity session tracker.

    Persists completed metrics to a JSONL file.  Only one activity can be
    active at a time; calling ``start_activity`` while another is active
    will stop the previous one first.

    Attributes:
        metrics_dir: Directory where activity.jsonl is stored.
    """

    def __init__(self, metrics_dir: Path | None = None) -> None:
        self._metrics_dir = metrics_dir or Path.cwd()
        self._lock = threading.Lock()
        self._active_start: float | None = None
        self._active_category: str | None = None
        self._active_description: str | None = None
        self._completed: list[ActivityMetric] = []
        self._load()

    # ------------------------------------------------------------------
    # Property helpers
    # ------------------------------------------------------------------

    @property
    def _activity_file(self) -> Path:
        """Path to the activity JSONL file."""
        return self._metrics_dir / _DEFAULT_ACTIVITY_FILE

    @property
    def is_active(self) -> bool:
        """Return True if an activity is currently being tracked."""
        return self._active_start is not None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_activity(self, category: str, description: str) -> None:
        """Start tracking an activity.

        Args:
            category: One of the ActivityCategory values.
            description: Human-readable description of the activity.

        Raises:
            ValueError: If category is not a valid ActivityCategory.
        """
        category_enum = ActivityCategory(category)

        with self._lock:
            # Stop any currently active activity first.
            if self._active_start is not None:
                self._stop_current_locked()

            self._active_start = time.time()
            self._active_category = category_enum.value
            self._active_description = description

    def stop_activity(self) -> ActivityMetric | None:
        """Stop the currently tracked activity and persist it.

        Returns:
            The completed ActivityMetric, or None if no activity was active.
        """
        with self._lock:
            return self._stop_current_locked()

    def reset(self) -> list[ActivityMetric]:
        """Return all tracked metrics and clear internal state.

        Returns:
            List of all completed ActivityMetric instances.
        """
        with self._lock:
            completed = list(self._completed)
            self._completed.clear()
            self._active_start = None
            self._active_category = None
            self._active_description = None
        return completed

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_activity_summary(self, since: float = 0) -> list[ActivityMetric]:
        """Return all completed metrics since the given timestamp.

        Metrics are returned sorted by timestamp (oldest first).

        Args:
            since: Only include metrics with timestamp >= this value.

        Returns:
            List of matching ActivityMetric instances.
        """
        with self._lock:
            metrics = [m for m in self._completed if m.timestamp >= since]
        return sorted(metrics, key=lambda m: m.timestamp)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load previously persisted metrics from the JSONL file."""
        activity_file = self._activity_file
        if not activity_file.exists():
            return

        try:
            lines = activity_file.read_text(encoding="utf-8").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                self._completed.append(ActivityMetric.from_dict(data))
        except Exception:
            logger.warning("Failed to load activity metrics from %s", activity_file, exc_info=True)

    def _persist(self, metric: ActivityMetric) -> None:
        """Append a single metric to the JSONL file."""
        activity_file = self._activity_file
        activity_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with activity_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(metric.to_dict()) + "\n")
        except Exception:
            logger.warning("Failed to persist activity metric to %s", activity_file, exc_info=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _stop_current_locked(self) -> ActivityMetric | None:
        """Stop the currently tracked activity (must hold self._lock)."""
        if self._active_start is None:
            return None

        duration = time.time() - self._active_start
        metric = ActivityMetric(
            timestamp=self._active_start,
            category=self._active_category or ActivityCategory.OTHER.value,
            duration_s=round(duration, 3),
            description=self._active_description or "",
        )

        self._completed.append(metric)
        self._persist(metric)

        self._active_start = None
        self._active_category = None
        self._active_description = None

        return metric


# ------------------------------------------------------------------
# Module-level convenience API (singleton pattern)
# ------------------------------------------------------------------

_default_session: ActivitySession | None = None
_session_lock = threading.Lock()


def _get_session() -> ActivitySession:
    """Return or create the default ActivitySession singleton."""
    global _default_session
    with _session_lock:
        if _default_session is None:
            _default_session = ActivitySession()
        return _default_session


def start_activity(category: str, description: str) -> None:
    """Start tracking an activity using the default session.

    Args:
        category: One of the ActivityCategory values.
        description: Human-readable description of the activity.

    Raises:
        ValueError: If category is not a valid ActivityCategory.
    """
    _get_session().start_activity(category, description)


def stop_activity() -> ActivityMetric | None:
    """Stop the currently tracked activity and return the metric.

    Returns:
        The completed ActivityMetric, or None if no activity was active.
    """
    return _get_session().stop_activity()


def get_activity_summary(since: float = 0) -> list[ActivityMetric]:
    """Return all completed activity metrics since the given timestamp.

    Args:
        since: Only include metrics with timestamp >= this value.

    Returns:
        List of matching ActivityMetric instances.
    """
    return _get_session().get_activity_summary(since)
