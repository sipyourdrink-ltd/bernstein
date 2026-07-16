"""Read ``bernstein doctor observe`` snapshots into evolution signals.

The daily observability workflow appends one JSON snapshot per day under
``docs/observability/snapshots/<YYYY-MM-DD>.json``. Every metric row carries a
``threshold_status`` (``ok|warn|fail``) and a ``numeric`` value. This module
turns the two most recent snapshots into signals the self-improvement loop can
consume:

* :func:`coverage_delta_fraction` - the signed Sonar coverage change as a
  fraction, matching the ``test_coverage_delta`` domain of
  ``RiskScorer.score_proposal``.
* :func:`detect_regressions` - security or coverage regressions worth an
  ``ImprovementOpportunity``.

Everything is best-effort and guarded: a missing directory, fewer than two
snapshots, or malformed JSON yields an empty / zero result rather than an
error. The self-improvement loop otherwise sees only internal cost and task
metrics; these functions feed it external repo-health signals.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Default location of the daily snapshot corpus, relative to the repo root.
DEFAULT_SNAPSHOTS_DIR = Path("docs/observability/snapshots")

#: ``(backend, metric)`` security signals and the severity of an increase.
_SECURITY_METRICS: dict[tuple[str, str], str] = {
    ("dt", "critical_vulns"): "high",
    ("dt", "high_vulns"): "high",
    ("code-scanning", "critical_alerts"): "high",
    ("code-scanning", "high_alerts"): "high",
    ("sonar", "vulnerabilities"): "medium",
    ("sonar", "security_hotspots"): "medium",
    ("dt", "medium_vulns"): "low",
    ("code-scanning", "open_alerts"): "low",
}

#: Coverage drop (percentage points) that warrants an opportunity.
_COVERAGE_DROP_PT = 1.0


@dataclass(frozen=True)
class ObservabilityRegression:
    """A security or coverage regression derived from two snapshots."""

    backend: str
    metric: str
    prev: float | None
    curr: float
    delta: float
    kind: str  # "security" | "coverage"
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
    dated.sort(key=lambda item: item[0])
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


def coverage_delta_fraction(snapshots_dir: Path = DEFAULT_SNAPSHOTS_DIR) -> float:
    """Return the signed Sonar coverage change as a fraction in ``[-1, 1]``.

    Positive means coverage improved. Sonar reports coverage in percent, so the
    percentage-point delta is divided by 100 to match the fraction domain of
    ``RiskScorer.score_proposal``. Returns ``0.0`` when the delta cannot be
    computed (no directory, fewer than two snapshots, coverage missing).
    """

    prev, curr = latest_two_snapshots(snapshots_dir)
    prev_cov = _metric_numeric(prev, "sonar", "coverage_pct")
    curr_cov = _metric_numeric(curr, "sonar", "coverage_pct")
    if prev_cov is None or curr_cov is None:
        return 0.0
    return (curr_cov - prev_cov) / 100.0


def detect_regressions(snapshots_dir: Path = DEFAULT_SNAPSHOTS_DIR) -> list[ObservabilityRegression]:
    """Return security or coverage regressions from the two latest snapshots.

    A regression requires a real day-over-day baseline: a metric present only
    in the newer snapshot (for example a backend that just gained credentials)
    is a first observation, not a regression, and is skipped.
    """

    prev, curr = latest_two_snapshots(snapshots_dir)
    if prev is None or curr is None:
        return []

    regressions: list[ObservabilityRegression] = []

    prev_cov = _metric_numeric(prev, "sonar", "coverage_pct")
    curr_cov = _metric_numeric(curr, "sonar", "coverage_pct")
    if prev_cov is not None and curr_cov is not None:
        delta = curr_cov - prev_cov
        if delta <= -_COVERAGE_DROP_PT:
            regressions.append(
                ObservabilityRegression(
                    backend="sonar",
                    metric="coverage_pct",
                    prev=prev_cov,
                    curr=curr_cov,
                    delta=delta,
                    kind="coverage",
                    severity="medium",
                )
            )

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
