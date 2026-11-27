"""Cost / latency / abandonment / blast-radius predictors.

Each predictor is a small, pure object that consumes a task description
plus an optional :class:`HistoricalTraces` view and returns a forecast.

Design constraints (issue #1374):

* Read-only: predictors only read from ``.sdd/traces`` and
  ``.sdd/metrics``; never write.
* Deterministic: a fixed seed produces a stable output for the same input.
* Cold-start safe: missing history falls back to a documented heuristic.
* No NumPy: ``statistics.quantiles`` covers everything we need.

The cost predictor delegates to :mod:`bernstein.core.cost.preflight` when
metrics history is available, so the simulator stays consistent with the
preflight banner shown on real runs.

The blast-radius predictor delegates to
:func:`bernstein.core.quality.blast_radius.score_change`, scoring on the
task's declared ``owned_files`` plus a synthetic diff body assembled from
the task description. This keeps risk scoring honest without requiring a
real diff.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from bernstein.core.cost.preflight import compute_band, load_history
from bernstein.core.quality.blast_radius import BlastRadiusScorer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.tasks.models import Task

__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "AbandonmentPredictor",
    "BlastRadiusPredictor",
    "CostPredictor",
    "HistoricalTraces",
    "LatencyPredictor",
    "load_traces",
]

logger = logging.getLogger(__name__)


DEFAULT_HISTORY_LIMIT: Final[int] = 50
"""Newest-N records consulted per (role, adapter) key."""

# Heuristic priors when no traces are available. These are calibrated to
# match the cold-start envelope used in :mod:`bernstein.core.cost.preflight`
# and the abandon-rate prior observed across the seed plans shipped with
# Bernstein (~5% abandon, 60s minimum task wall-clock).
_COLD_ABANDON_PRIOR: Final[float] = 0.05
_COLD_LATENCY_P50: Final[float] = 60.0
_COLD_LATENCY_P90: Final[float] = 180.0

# Mapping from role to canonical criterion bucket. Used by the runner to
# build the criterion-profile bias chart. Unknown roles fall back to
# ``"quality"`` (most roles in the catalog are quality-leaning by default).
_ROLE_CRITERION: Final[dict[str, str]] = {
    "backend": "quality",
    "frontend": "speed",
    "qa": "quality",
    "security": "safety",
    "adversary": "safety",
    "reviewer": "safety",
    "ci-fixer": "speed",
    "devops": "speed",
    "docs": "cost",
    "manager": "cost",
    "architect": "quality",
    "analyst": "quality",
    "ml-engineer": "quality",
    "prompt-engineer": "cost",
    "retrieval": "speed",
    "resolver": "safety",
    "visionary": "cost",
    "vp": "cost",
}


def role_criterion(role: str) -> str:
    """Return the canonical criterion bucket for ``role``.

    Falls back to ``"quality"`` for unknown roles.
    """
    return _ROLE_CRITERION.get(role.strip().lower(), "quality")


# ---------------------------------------------------------------------------
# Historical traces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalTraces:
    """In-memory view of historical agent traces.

    Built from ``.sdd/traces/*.jsonl`` records. Only the fields the
    simulator needs are extracted; everything else is ignored. The
    structure is keyed by ``(role, adapter)`` so cold-start fallback can
    be applied per dimension.

    Attributes:
        abandon_rates: Map from ``(role, adapter)`` to observed abandon
            probability in [0, 1].
        latency_samples: Map from ``(role, adapter)`` to a tuple of
            wall-clock samples (seconds). Empty tuple means cold-start.
        sample_count: Total number of trace records parsed across all
            keys.
    """

    abandon_rates: dict[tuple[str, str], float] = field(default_factory=dict[tuple[str, str], float])
    latency_samples: dict[tuple[str, str], tuple[float, ...]] = field(
        default_factory=dict[tuple[str, str], tuple[float, ...]]
    )
    sample_count: int = 0

    @property
    def is_empty(self) -> bool:
        return self.sample_count == 0


def _coerce_str(raw: object) -> str:
    if isinstance(raw, str):
        return raw.strip().lower()
    return ""


def _coerce_float(raw: object) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


def load_traces(traces_dir: Path | None, *, limit: int = DEFAULT_HISTORY_LIMIT) -> HistoricalTraces:
    """Load historical traces into a :class:`HistoricalTraces` view.

    Reads every ``*.jsonl`` file under ``traces_dir``. Each line is
    expected to be a JSON object with at minimum a ``role`` field; the
    optional fields the simulator uses are:

    * ``adapter`` / ``cli`` / ``model`` - adapter id
    * ``abandoned`` - boolean
    * ``status`` - ``"abandoned"`` / ``"completed"`` etc.
    * ``latency_seconds`` / ``duration_s`` - wall-clock seconds

    Missing or malformed records are skipped silently.
    """
    abandon_counters: dict[tuple[str, str], list[int]] = {}
    latency_buckets: dict[tuple[str, str], list[float]] = {}
    total = 0

    if traces_dir is None or not traces_dir.exists() or not traces_dir.is_dir():
        return HistoricalTraces()

    for trace_file in sorted(traces_dir.glob("*.jsonl")):
        try:
            text = trace_file.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            logger.debug("simulate: trace file unreadable: %s (%s)", trace_file, exc)
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            record: dict[str, object] = {
                str(k): v  # type: ignore[reportUnknownArgumentType, reportUnknownVariableType]
                for k, v in parsed.items()  # type: ignore[reportUnknownVariableType]
            }
            role = _coerce_str(record.get("role"))
            if not role:
                continue
            adapter = (
                _coerce_str(record.get("adapter"))
                or _coerce_str(record.get("cli"))
                or _coerce_str(record.get("model"))
                or "mock"
            )
            key = (role, adapter)
            counters = abandon_counters.setdefault(key, [0, 0])  # [abandoned, total]
            abandoned = bool(record.get("abandoned")) or _coerce_str(record.get("status")) == "abandoned"
            counters[1] += 1
            if abandoned:
                counters[0] += 1
            latency = _coerce_float(record.get("latency_seconds"))
            if latency is None:
                latency = _coerce_float(record.get("duration_s"))
            if latency is not None:
                bucket = latency_buckets.setdefault(key, [])
                bucket.append(latency)
            total += 1

    abandon_rates: dict[tuple[str, str], float] = {}
    for key, (bad, all_) in abandon_counters.items():
        if all_ <= 0:
            continue
        abandon_rates[key] = bad / all_

    latency_samples: dict[tuple[str, str], tuple[float, ...]] = {}
    for key, samples in latency_buckets.items():
        # Newest-last; trim to ``limit``.
        trimmed = samples[-limit:] if len(samples) > limit else samples
        latency_samples[key] = tuple(trimmed)

    return HistoricalTraces(
        abandon_rates=abandon_rates,
        latency_samples=latency_samples,
        sample_count=total,
    )


# ---------------------------------------------------------------------------
# Cost predictor
# ---------------------------------------------------------------------------


def _percentile(samples: Sequence[float], q: float) -> float:
    """Return ``q``-quantile (``q`` in [0, 1]) via stdlib only."""
    if not samples:
        return 0.0
    if len(samples) == 1:
        return samples[0]
    idx = round(q * 100) - 1
    idx = max(0, min(98, idx))
    cuts = statistics.quantiles(list(samples), n=100, method="inclusive")
    return cuts[idx]


@dataclass(frozen=True, slots=True)
class CostPredictor:
    """Predict per-task cost band in USD.

    Delegates to :mod:`bernstein.core.cost.preflight` so the simulation
    stays consistent with the real preflight banner. Cold-start path uses
    the same legacy heuristic, keyed off ``model``.

    Attributes:
        metrics_dir: Optional path to ``.sdd/metrics``. ``None`` forces
            cold-start.
        default_model: Model id used for the cold-start heuristic when a
            task does not declare its own.
        default_adapter: Adapter id used when a task does not declare
            ``cli``.
        history_limit: Max samples per (role, adapter) pair.
    """

    metrics_dir: Path | None = None
    default_model: str = "claude-sonnet-4"
    default_adapter: str = "mock"
    history_limit: int = DEFAULT_HISTORY_LIMIT

    def predict(self, task: Task) -> tuple[float, float, bool]:
        """Return ``(p50, p90, cold_start)`` for ``task``."""
        role = task.role or "backend"
        adapter = (task.cli or self.default_adapter).strip().lower()
        model = (task.model or self.default_model).strip()
        if self.metrics_dir is None:
            # Force the heuristic path without touching disk.
            band = compute_band(
                role=role,
                adapter=adapter,
                model=model,
                task_count=1,
                metrics_dir=Path("/__simulate_no_metrics__"),
                history_limit=self.history_limit,
            )
            return (band.p50, band.p90, band.cold_start)
        band = compute_band(
            role=role,
            adapter=adapter,
            model=model,
            task_count=1,
            metrics_dir=self.metrics_dir,
            history_limit=self.history_limit,
        )
        return (band.p50, band.p90, band.cold_start)

    def history_count(self, *, role: str, adapter: str) -> int:
        """Return the number of historical cost samples for ``(role, adapter)``."""
        if self.metrics_dir is None:
            return 0
        return len(
            load_history(
                self.metrics_dir / "cost.jsonl",
                role=role,
                adapter=adapter,
                limit=self.history_limit,
            )
        )


# ---------------------------------------------------------------------------
# Latency predictor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LatencyPredictor:
    """Predict per-task wall-clock latency (seconds).

    History path: percentile over historical samples for ``(role, adapter)``.
    Cold-start: uniform prior using the task's ``estimated_minutes``
    field scaled into seconds, with a documented minimum.

    Attributes:
        traces: :class:`HistoricalTraces` view.
        floor_p50: Minimum p50 seconds for cold-start.
        floor_p90: Minimum p90 seconds for cold-start.
    """

    traces: HistoricalTraces = field(default_factory=HistoricalTraces)
    floor_p50: float = _COLD_LATENCY_P50
    floor_p90: float = _COLD_LATENCY_P90

    def predict(self, task: Task, *, default_adapter: str = "mock") -> tuple[float, float]:
        """Return ``(p50, p90)`` seconds for ``task``."""
        role = (task.role or "backend").strip().lower()
        adapter = (task.cli or default_adapter).strip().lower()
        samples = self.traces.latency_samples.get((role, adapter), ())
        if samples:
            p50 = _percentile(samples, 0.5)
            p90 = _percentile(samples, 0.9)
            if p90 < p50:
                p90 = p50
            return (round(p50, 2), round(p90, 2))
        # Cold-start: scale from estimated_minutes, floor at the prior.
        minutes = float(max(1, getattr(task, "estimated_minutes", 30)))
        base = minutes * 60.0
        p50 = max(self.floor_p50, base * 0.6)
        p90 = max(self.floor_p90, base * 1.2)
        return (round(p50, 2), round(p90, 2))


# ---------------------------------------------------------------------------
# Abandonment predictor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AbandonmentPredictor:
    """Predict abandon probability for a task in [0, 1].

    History path: fraction of past records for ``(role, adapter)`` that
    ended in ``status == "abandoned"`` (or carried ``abandoned: true``).

    Cold-start: returns ``_COLD_ABANDON_PRIOR``. The prior is intentionally
    small but non-zero so a downstream consumer always sees a "this might
    fail" signal even on green workspaces.

    Attributes:
        traces: :class:`HistoricalTraces` view.
        cold_prior: Cold-start abandon probability.
    """

    traces: HistoricalTraces = field(default_factory=HistoricalTraces)
    cold_prior: float = _COLD_ABANDON_PRIOR

    def predict(self, task: Task, *, default_adapter: str = "mock") -> float:
        """Return abandon probability for ``task`` in [0, 1]."""
        role = (task.role or "backend").strip().lower()
        adapter = (task.cli or default_adapter).strip().lower()
        rate = self.traces.abandon_rates.get((role, adapter))
        if rate is None:
            return self.cold_prior
        # Clamp defensively; corrupt traces could push the ratio out.
        return max(0.0, min(1.0, rate))


# ---------------------------------------------------------------------------
# Blast-radius predictor
# ---------------------------------------------------------------------------


@dataclass
class BlastRadiusPredictor:
    """Predict the blast-radius score for a task in [0, 1].

    Reuses the production scorer so this estimate matches the gate that
    would actually evaluate the change on a real run. We construct a
    "synthetic" diff body from the task description so content detectors
    still get a chance to fire on obvious destructive keywords
    (``DROP TABLE``, ``rm -rf`` etc.).

    Attributes:
        scorer: Underlying production scorer.
    """

    scorer: BlastRadiusScorer = field(default_factory=BlastRadiusScorer)

    def predict(self, task: Task) -> float:
        """Return the blast-radius score for ``task`` in [0, 1]."""
        files = tuple(task.owned_files or ())
        diff_text = task.description or ""
        report = self.scorer.score(files=files, diff_text=diff_text)
        return float(report.score)
