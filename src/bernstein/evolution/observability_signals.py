"""Read ``bernstein doctor observe`` snapshots into evolution signals.

The daily observability workflow appends one JSON snapshot per day under
``docs/_internal/observability/snapshots/<YYYY-MM-DD>.json``. Every metric row carries a
``threshold_status`` (``ok|warn|fail``) and a ``numeric`` value. This module
turns the two most recent snapshots into signals the self-improvement loop can
consume:

* :func:`detect_regressions` - security regressions worth an
  ``ImprovementOpportunity``.

Everything is best-effort and guarded: a missing directory, fewer than two
snapshots, or malformed JSON yields an empty / zero result rather than an
error. The self-improvement loop otherwise sees only internal cost and task
metrics; these functions feed it external repo-health signals.
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
    ("dt", "critical_vulns"): "high",
    ("dt", "high_vulns"): "high",
    ("code-scanning", "critical_alerts"): "high",
    ("code-scanning", "high_alerts"): "high",
    ("dt", "medium_vulns"): "low",
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


def detect_regressions(snapshots_dir: Path = DEFAULT_SNAPSHOTS_DIR) -> list[ObservabilityRegression]:
    """Return security regressions from the two latest snapshots.

    A regression requires a real day-over-day baseline: a metric present only
    in the newer snapshot (for example a backend that just gained credentials)
    is a first observation, not a regression, and is skipped.
    """

    prev, curr = latest_two_snapshots(snapshots_dir)
    if prev is None or curr is None:
        return []

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

    return regressions
