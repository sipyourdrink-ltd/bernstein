"""Read ``bernstein doctor observe`` snapshots into evolution signals.

The daily observability workflow appends one JSON snapshot per day under
``docs/_internal/observability/snapshots/<YYYY-MM-DD>.json``. Every metric row carries a
``threshold_status`` (``ok|warn|fail``) and a ``numeric`` value. This module
turns the two most recent snapshots into signals the self-improvement loop can
consume:

* :func:`detect_regressions` - security regressions worth an
  ``ImprovementOpportunity``, wrapped in an :class:`ObservabilityScan`
  that says whether a baseline comparison actually ran.

Everything is best-effort and guarded: a missing directory, fewer than two
snapshots, or malformed JSON never raises. A scan without a baseline is
reported as ``baseline_present=False`` so callers can tell "nothing was
compared" apart from "compared and found nothing". The self-improvement
loop otherwise sees only internal cost and task metrics; these functions
feed it external repo-health signals.
"""

from __future__ import annotations

import datetime as dt
import json
import operator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Default location of the daily snapshot corpus, relative to the repo root.
DEFAULT_SNAPSHOTS_DIR = Path("docs/_internal/observability/snapshots")

#: ``(backend, metric)`` security signals and the severity of an increase.
_SECURITY_METRICS: dict[tuple[str, str], str] = {
    ("code-scanning", "critical_alerts"): "high",
    ("code-scanning", "high_alerts"): "high",
    ("code-scanning", "open_alerts"): "low",
}


@dataclass(frozen=True)
class ObservabilityRegression:
    """A security regression derived from two snapshots."""

    backend: str
    metric: str
    prev: float | None
    curr: float
    delta: float
    kind: str  # "security"
    severity: str  # "high" | "medium" | "low"


@dataclass(frozen=True)
class ObservabilityScan:
    """Result of a snapshot scan, explicit about whether a comparison ran.

    ``baseline_present=False`` with empty ``regressions`` means no
    day-over-day comparison was performed; it is not a clean result.
    """

    baseline_present: bool
    regressions: list[ObservabilityRegression]


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def latest_two_snapshots(
    snapshots_dir: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(prev, curr)`` for the two most recent dated snapshots."""

    if not snapshots_dir.exists():
        return (None, None)
    dated: list[tuple[dt.date, Path]] = []
    for path in snapshots_dir.glob("*.json"):
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        dated.append((day, path))
    dated.sort(key=operator.itemgetter(0))
    if not dated:
        return (None, None)
    curr = _load(dated[-1][1])
    prev = _load(dated[-2][1]) if len(dated) >= 2 else None
    return (prev, curr)


def _metric_numeric(payload: dict[str, Any] | None, backend: str, metric: str) -> float | None:
    """Return the numeric value of ``backend.metric`` in a snapshot, or None."""

    if not payload:
        return None
    for b in payload.get("backends") or []:
        if b.get("backend") != backend:
            continue
        for m in b.get("metrics") or []:
            if m.get("name") == metric:
                value = m.get("numeric")
                if isinstance(value, bool):
                    return None
                if isinstance(value, (int, float)):
                    return float(value)
        return None
    return None


def detect_regressions(snapshots_dir: Path = DEFAULT_SNAPSHOTS_DIR) -> ObservabilityScan:
    """Scan the two latest snapshots for security regressions.

    A regression requires a real day-over-day baseline: a metric present only
    in the newer snapshot (for example a backend that just gained credentials)
    is a first observation, not a regression, and is skipped. When fewer than
    two readable snapshots exist the scan reports ``baseline_present=False``
    instead of pretending a comparison found nothing.
    """

    prev, curr = latest_two_snapshots(snapshots_dir)
    if prev is None or curr is None:
        return ObservabilityScan(baseline_present=False, regressions=[])

    regressions: list[ObservabilityRegression] = []

    for (backend, metric), severity in _SECURITY_METRICS.items():
        prev_num = _metric_numeric(prev, backend, metric)
        curr_num = _metric_numeric(curr, backend, metric)
        if prev_num is None or curr_num is None:
            continue
        delta = curr_num - prev_num
        if delta > 0:
            regressions.append(
                ObservabilityRegression(
                    backend=backend,
                    metric=metric,
                    prev=prev_num,
                    curr=curr_num,
                    delta=delta,
                    kind="security",
                    severity=severity,
                )
            )

    return ObservabilityScan(baseline_present=True, regressions=regressions)
