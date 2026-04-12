"""Adaptive timeout calculation based on task characteristics.

Computes per-task timeouts from scope, complexity, model speed, historical
completion data, and file count instead of using a single static value.

Usage::

    from bernstein.core.orchestration.adaptive_timeout import estimate_timeout
    est = estimate_timeout(task, model="sonnet")
    spawner.set_timeout(est.timeout_s)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.defaults import TASK

if TYPE_CHECKING:
    from bernstein.core.tasks.models import Complexity, Scope, Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMPLEXITY_MULTIPLIERS: dict[str, float] = {
    "low": 0.7,
    "medium": 1.0,
    "high": 1.5,
}

_MODEL_SPEED_FACTORS: dict[str, float] = {
    "haiku": 0.5,
    "sonnet": 1.0,
    "opus": 1.5,
}

_DEFAULT_MIN_TIMEOUT_S: float = 300.0
_DEFAULT_MAX_TIMEOUT_S: float = 7200.0
_HISTORICAL_HEADROOM: float = 1.5  # multiply historical avg by this factor
_PER_FILE_SECONDS: float = 30.0

DEFAULT_ARCHIVE_PATH = Path(".sdd/archive/tasks.jsonl")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeoutFactors:
    """Individual factors that contributed to a timeout estimate.

    Attributes:
        complexity_score: Normalised complexity (0.0--1.0).
        scope_multiplier: Base seconds derived from task scope.
        model_speed_factor: Speed multiplier for the chosen model.
        historical_avg_s: Mean completion seconds of similar past tasks,
            or ``None`` when no history is available.
        file_count: Number of files in the task's ``owned_files``.
    """

    complexity_score: float
    scope_multiplier: float
    model_speed_factor: float
    historical_avg_s: float | None
    file_count: int


@dataclass(frozen=True)
class TimeoutEstimate:
    """Result of adaptive timeout calculation.

    Attributes:
        timeout_s: Recommended timeout in seconds.
        min_timeout_s: Lower bound enforced by ``clamp_timeout``.
        max_timeout_s: Upper bound enforced by ``clamp_timeout``.
        confidence: How confident the estimate is (0.0--1.0).
            Higher when historical data is available.
        factors: The individual factors used in the calculation.
    """

    timeout_s: float
    min_timeout_s: float
    max_timeout_s: float
    confidence: float
    factors: TimeoutFactors


# ---------------------------------------------------------------------------
# Historical lookup
# ---------------------------------------------------------------------------


def get_historical_average(
    role: str,
    scope: str,
    complexity: str,
    archive_path: Path = DEFAULT_ARCHIVE_PATH,
) -> float | None:
    """Look up the average duration of similar completed tasks.

    Reads the JSONL task archive and filters for records that match the
    given *role*.  Since the archive does not store ``scope`` or
    ``complexity``, the match is by role only; the caller should treat
    the result as an approximation.

    Args:
        role: Agent role (e.g. ``"backend"``, ``"qa"``).
        scope: Task scope (unused for matching but kept for API symmetry).
        complexity: Task complexity (unused for matching but kept for API symmetry).
        archive_path: Path to the JSONL archive file.

    Returns:
        Mean duration in seconds of matching records, or ``None`` when no
        matching records exist.
    """
    if not archive_path.exists():
        return None

    durations: list[float] = []
    try:
        with archive_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("status") != "done":
                    continue
                if record.get("role") != role:
                    continue
                dur = record.get("duration_seconds")
                if isinstance(dur, (int, float)) and dur > 0:
                    durations.append(float(dur))
    except OSError:
        logger.debug("adaptive_timeout: cannot read archive %s", archive_path)
        return None

    if not durations:
        return None
    return sum(durations) / len(durations)


# ---------------------------------------------------------------------------
# Core estimation
# ---------------------------------------------------------------------------


def _scope_base_timeout(scope: Scope) -> float:
    """Return the base timeout seconds for a scope from defaults.

    Falls back to the medium scope timeout when the scope string is
    not found in the defaults mapping.
    """
    return float(TASK.scope_timeout_s.get(scope.value, TASK.scope_timeout_s["medium"]))


def _complexity_multiplier(complexity: Complexity) -> float:
    """Return the multiplier for a complexity level."""
    return _COMPLEXITY_MULTIPLIERS.get(complexity.value, 1.0)


def _model_speed_factor(model: str | None) -> float:
    """Return the speed factor for a model name.

    Unknown models default to the ``sonnet`` factor (1.0).
    """
    if model is None:
        return 1.0
    return _MODEL_SPEED_FACTORS.get(model.lower(), 1.0)


def _complexity_score(complexity: Complexity) -> float:
    """Normalise complexity to a 0.0--1.0 score."""
    mapping: dict[str, float] = {
        "low": 0.2,
        "medium": 0.5,
        "high": 1.0,
    }
    return mapping.get(complexity.value, 0.5)


def estimate_timeout(
    task: Task,
    model: str | None = None,
    historical_data: float | None = None,
    archive_path: Path = DEFAULT_ARCHIVE_PATH,
) -> TimeoutEstimate:
    """Compute an adaptive timeout from task characteristics.

    The timeout is derived from four additive/multiplicative components:

    1. **Base** -- scope timeout from ``defaults.py``
       (small=900, medium=1800, large=3600).
    2. **Complexity multiplier** -- low=0.7, medium=1.0, high=1.5.
    3. **Model speed factor** -- haiku=0.5, sonnet=1.0, opus=1.5.
    4. **Historical calibration** -- if similar tasks completed in avg
       *X* seconds, use ``X * 1.5`` instead of the computed value when
       that is larger.
    5. **File count** -- +30 s per file in ``owned_files``.

    Args:
        task: The task to estimate a timeout for.
        model: Model name hint (``"haiku"``, ``"sonnet"``, ``"opus"``).
            Falls back to ``task.model`` when ``None``.
        historical_data: Pre-computed historical average in seconds.
            When ``None``, the archive is consulted automatically.
        archive_path: Path to the JSONL archive for historical lookup.

    Returns:
        A clamped ``TimeoutEstimate`` with factors breakdown.
    """
    resolved_model = model or task.model
    base_s = _scope_base_timeout(task.scope)
    cmult = _complexity_multiplier(task.complexity)
    mfactor = _model_speed_factor(resolved_model)
    file_count = len(task.owned_files)

    computed_s = base_s * cmult * mfactor + file_count * _PER_FILE_SECONDS

    # Historical calibration
    hist_avg: float | None = historical_data
    if hist_avg is None:
        hist_avg = get_historical_average(
            role=task.role,
            scope=task.scope.value,
            complexity=task.complexity.value,
            archive_path=archive_path,
        )

    confidence = 0.5  # default: no history
    if hist_avg is not None:
        historical_timeout = hist_avg * _HISTORICAL_HEADROOM
        computed_s = max(computed_s, historical_timeout)
        confidence = 0.8

    factors = TimeoutFactors(
        complexity_score=_complexity_score(task.complexity),
        scope_multiplier=base_s,
        model_speed_factor=mfactor,
        historical_avg_s=hist_avg,
        file_count=file_count,
    )

    return clamp_timeout(
        TimeoutEstimate(
            timeout_s=computed_s,
            min_timeout_s=_DEFAULT_MIN_TIMEOUT_S,
            max_timeout_s=_DEFAULT_MAX_TIMEOUT_S,
            confidence=confidence,
            factors=factors,
        ),
    )


# ---------------------------------------------------------------------------
# Clamping
# ---------------------------------------------------------------------------


def clamp_timeout(
    estimate: TimeoutEstimate,
    min_s: float = _DEFAULT_MIN_TIMEOUT_S,
    max_s: float = _DEFAULT_MAX_TIMEOUT_S,
) -> TimeoutEstimate:
    """Enforce minimum and maximum bounds on a timeout estimate.

    Args:
        estimate: The raw timeout estimate.
        min_s: Minimum allowed timeout in seconds (default 300).
        max_s: Maximum allowed timeout in seconds (default 7200).

    Returns:
        A new ``TimeoutEstimate`` with ``timeout_s`` clamped to
        ``[min_s, max_s]`` and bounds fields updated.
    """
    clamped = max(min_s, min(estimate.timeout_s, max_s))
    return TimeoutEstimate(
        timeout_s=clamped,
        min_timeout_s=min_s,
        max_timeout_s=max_s,
        confidence=estimate.confidence,
        factors=estimate.factors,
    )
