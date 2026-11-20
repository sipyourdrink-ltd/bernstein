"""Calibration log + Brier score for router and judge decisions.

This module measures the calibration quality of probability and confidence
outputs from the bandit router, LLM judge, and any other Bernstein component
that emits a probability paired with an eventually-observed binary outcome.

Three core primitives are exported:

* :func:`BrierScore` - mean squared error between predicted probability and
  observed outcome. Lower is better; range is ``[0, 1]``.
* :func:`expected_calibration_error` - weighted average bucket gap between
  predicted-probability mean and observed-outcome mean (the classic 10-bin
  ECE reference implementation).
* :func:`reliability_diagram_data` - per-bucket data for a reliability plot
  (predicted mean, observed mean, count, lower edge, upper edge).

Additionally, helpers manage the on-disk JSONL log at
``.sdd/metrics/calibration.jsonl`` and produce a structured report consumed
by the ``bernstein eval calibration report`` CLI surface.

The implementation is pure-Python with no external dependencies, type-checked
under pyright strict, and ruff-clean. All numeric routines handle the
zero-prediction, single-prediction, and perfectly-calibrated edge cases
deterministically.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

__all__ = [
    "DEFAULT_LOG_PATH",
    "BrierScore",
    "CalibrationLogError",
    "CalibrationRecord",
    "CalibrationReport",
    "ReliabilityBucket",
    "compute_brier",
    "compute_report",
    "expected_calibration_error",
    "load_log",
    "log_decision",
    "parse_duration",
    "reliability_diagram_data",
]


DEFAULT_LOG_PATH: Final[Path] = Path(".sdd/metrics/calibration.jsonl")
"""Default on-disk location for the calibration JSONL log."""

_DEFAULT_BIN_COUNT: Final[int] = 10
"""Default number of reliability buckets - the standard 10-bin ECE config."""

_DURATION_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(\d+)\s*([smhdw])\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Errors and dataclasses
# ---------------------------------------------------------------------------


class CalibrationLogError(ValueError):
    """Raised when the calibration log contains malformed or invalid data."""


@dataclass(frozen=True, slots=True)
class CalibrationRecord:
    """One predicted-probability/observed-outcome pair.

    Attributes:
        timestamp: Unix epoch seconds at which the decision was logged.
        decision_kind: A categorical label (e.g. ``"model_route"``, ``"judge"``).
        policy_path: Optional path or identifier of the policy that emitted
            the probability (e.g. ``"bandit/v3"``).
        predicted_prob: Probability that the decision will be a win,
            in the closed interval ``[0.0, 1.0]``.
        observed_outcome: Binary outcome - ``True`` for win, ``False`` for loss.
        decision_id: Optional identifier for joining with external logs.
        metadata: Optional auxiliary fields preserved on round-trip.
    """

    timestamp: float
    decision_kind: str
    policy_path: str
    predicted_prob: float
    observed_outcome: bool
    decision_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=lambda: cast("dict[str, Any]", {}))

    def __post_init__(self) -> None:
        """Validate the record at construction time."""
        if math.isnan(self.predicted_prob):
            msg = "predicted_prob must not be NaN"
            raise CalibrationLogError(msg)
        if math.isinf(self.predicted_prob):
            msg = "predicted_prob must be finite"
            raise CalibrationLogError(msg)
        if not 0.0 <= self.predicted_prob <= 1.0:
            msg = f"predicted_prob {self.predicted_prob!r} outside [0, 1]"
            raise CalibrationLogError(msg)
        # We *intentionally* keep this isinstance check despite the static
        # type - callers reaching us from JSON deserialisation may slip in
        # an ``int``; we want to reject that rather than silently coerce.
        if type(self.observed_outcome) is not bool:
            msg = "observed_outcome must be a bool"
            raise CalibrationLogError(msg)
        if not self.decision_kind:
            msg = "decision_kind must be a non-empty string"
            raise CalibrationLogError(msg)


@dataclass(frozen=True, slots=True)
class ReliabilityBucket:
    """One bucket of a reliability diagram.

    Attributes:
        lower: Inclusive lower bound of the bucket on the predicted scale.
        upper: Exclusive upper bound (inclusive only for the top bucket).
        count: Number of predictions that landed in this bucket.
        predicted_mean: Mean predicted probability of those predictions.
        observed_mean: Empirical mean outcome (1.0 = perfect win-rate).
    """

    lower: float
    upper: float
    count: int
    predicted_mean: float
    observed_mean: float


@dataclass(frozen=True, slots=True)
class BrierScore:
    """A Brier score paired with metadata for downstream reporting.

    Attributes:
        score: Mean squared error in ``[0, 1]`` between predicted probability
            and observed outcome - lower is better.
        sample_count: Number of predictions used to compute the score.
    """

    score: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """End-to-end calibration report for a window of decisions.

    Attributes:
        decisions: Total number of decisions considered.
        brier: Brier score for the window - ``None`` when ``decisions == 0``.
        ece: Expected calibration error - ``None`` when ``decisions == 0``.
        buckets: Reliability diagram buckets (always returned, may be empty).
        decision_kind: Optional filter that produced this report.
        since: Optional duration that produced this report (e.g. ``"7d"``).
    """

    decisions: int
    brier: float | None
    ece: float | None
    buckets: tuple[ReliabilityBucket, ...]
    decision_kind: str | None = None
    since: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the report."""
        return {
            "decisions": self.decisions,
            "brier": self.brier,
            "ece": self.ece,
            "buckets": [asdict(b) for b in self.buckets],
            "decision_kind": self.decision_kind,
            "since": self.since,
        }


# ---------------------------------------------------------------------------
# Numeric primitives
# ---------------------------------------------------------------------------


def _check_pairs(predicted: Sequence[float], observed: Sequence[bool]) -> None:
    """Validate paired probability/outcome inputs.

    Args:
        predicted: Sequence of probabilities in ``[0, 1]``.
        observed: Sequence of booleans matching ``predicted`` in length.

    Raises:
        CalibrationLogError: If lengths mismatch, predictions are non-finite,
            or predictions fall outside ``[0, 1]``.
    """
    if len(predicted) != len(observed):
        msg = f"length mismatch: predicted={len(predicted)} observed={len(observed)}"
        raise CalibrationLogError(msg)
    for i, p in enumerate(predicted):
        if math.isnan(p):
            msg = f"predicted[{i}] is NaN"
            raise CalibrationLogError(msg)
        if math.isinf(p):
            msg = f"predicted[{i}] is infinite"
            raise CalibrationLogError(msg)
        if not 0.0 <= p <= 1.0:
            msg = f"predicted[{i}]={p!r} outside [0, 1]"
            raise CalibrationLogError(msg)


def compute_brier(predicted: Sequence[float], observed: Sequence[bool]) -> BrierScore:
    """Compute the binary Brier score.

    The Brier score is the mean squared error between each predicted
    probability and the corresponding observed binary outcome (encoded as
    ``1.0`` for ``True``, ``0.0`` for ``False``).

    Args:
        predicted: Predicted probabilities, each in ``[0, 1]``.
        observed: Matching observed boolean outcomes.

    Returns:
        A :class:`BrierScore` with the mean squared error and sample count.

    Raises:
        CalibrationLogError: On length mismatch or invalid prediction value.
    """
    _check_pairs(predicted, observed)
    if not predicted:
        return BrierScore(score=0.0, sample_count=0)
    total = 0.0
    for p, o in zip(predicted, observed, strict=True):
        target = 1.0 if o else 0.0
        diff = p - target
        total += diff * diff
    return BrierScore(score=total / len(predicted), sample_count=len(predicted))


def reliability_diagram_data(
    predicted: Sequence[float],
    observed: Sequence[bool],
    *,
    bin_count: int = _DEFAULT_BIN_COUNT,
) -> tuple[ReliabilityBucket, ...]:
    """Compute per-bucket data for a reliability diagram.

    Args:
        predicted: Predicted probabilities, each in ``[0, 1]``.
        observed: Matching observed boolean outcomes.
        bin_count: Number of equal-width buckets across ``[0, 1]``.

    Returns:
        A tuple of :class:`ReliabilityBucket` entries - one per bucket,
        in monotonically non-decreasing ``lower`` order. Empty buckets are
        included with ``count=0``, ``predicted_mean=lower``,
        ``observed_mean=0.0`` so the diagram retains its axis structure.

    Raises:
        CalibrationLogError: On length mismatch or invalid prediction value.
        ValueError: If ``bin_count`` is not at least 1.
    """
    if bin_count < 1:
        msg = f"bin_count must be >= 1, got {bin_count}"
        raise ValueError(msg)
    _check_pairs(predicted, observed)
    width = 1.0 / bin_count
    lower_edges = [i * width for i in range(bin_count)]
    sums_pred: list[float] = [0.0] * bin_count
    sums_obs: list[float] = [0.0] * bin_count
    counts: list[int] = [0] * bin_count
    for p, o in zip(predicted, observed, strict=True):
        idx = bisect_right(lower_edges, p) - 1
        sums_pred[idx] += p
        sums_obs[idx] += 1.0 if o else 0.0
        counts[idx] += 1
    out: list[ReliabilityBucket] = []
    for i, lower in enumerate(lower_edges):
        upper = 1.0 if i == bin_count - 1 else lower_edges[i + 1]
        if counts[i] == 0:
            out.append(
                ReliabilityBucket(
                    lower=lower,
                    upper=upper,
                    count=0,
                    predicted_mean=lower,
                    observed_mean=0.0,
                )
            )
        else:
            out.append(
                ReliabilityBucket(
                    lower=lower,
                    upper=upper,
                    count=counts[i],
                    predicted_mean=sums_pred[i] / counts[i],
                    observed_mean=sums_obs[i] / counts[i],
                )
            )
    return tuple(out)


def expected_calibration_error(
    predicted: Sequence[float],
    observed: Sequence[bool],
    *,
    bin_count: int = _DEFAULT_BIN_COUNT,
) -> float:
    """Compute the Expected Calibration Error (ECE).

    ECE is the count-weighted mean absolute gap between bucket-mean
    predicted probability and bucket-mean observed outcome.

    Args:
        predicted: Predicted probabilities, each in ``[0, 1]``.
        observed: Matching observed boolean outcomes.
        bin_count: Number of equal-width buckets across ``[0, 1]``.

    Returns:
        A scalar in ``[0, 1]``. Returns ``0.0`` when no predictions are
        supplied - there is no error to report.

    Raises:
        CalibrationLogError: On length mismatch or invalid prediction value.
        ValueError: If ``bin_count`` is not at least 1.
    """
    buckets = reliability_diagram_data(predicted, observed, bin_count=bin_count)
    total = sum(b.count for b in buckets)
    if total == 0:
        return 0.0
    err = 0.0
    for b in buckets:
        if b.count == 0:
            continue
        err += (b.count / total) * abs(b.predicted_mean - b.observed_mean)
    return err


# ---------------------------------------------------------------------------
# Log I/O
# ---------------------------------------------------------------------------


def _record_to_json(record: CalibrationRecord) -> str:
    """Serialize a record to a single JSONL line."""
    payload: dict[str, Any] = {
        "ts": record.timestamp,
        "decision_kind": record.decision_kind,
        "policy_path": record.policy_path,
        "predicted_prob": record.predicted_prob,
        "observed_outcome": record.observed_outcome,
    }
    if record.decision_id is not None:
        payload["decision_id"] = record.decision_id
    if record.metadata:
        payload["metadata"] = record.metadata
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _record_from_json(line: str) -> CalibrationRecord:
    """Parse one JSONL line into a record."""
    try:
        raw_any: object = json.loads(line)
    except json.JSONDecodeError as exc:
        msg = f"invalid JSON in calibration log: {exc.msg}"
        raise CalibrationLogError(msg) from exc
    if not isinstance(raw_any, dict):
        msg = "calibration log entry must be a JSON object"
        raise CalibrationLogError(msg)
    raw = cast("dict[str, object]", raw_any)
    try:
        outcome_raw: object = raw["observed_outcome"]
    except KeyError as exc:
        msg = "calibration record missing 'observed_outcome'"
        raise CalibrationLogError(msg) from exc
    if not isinstance(outcome_raw, bool):
        msg = "observed_outcome must be a JSON boolean"
        raise CalibrationLogError(msg)
    try:
        ts = float(raw["ts"])  # type: ignore[arg-type]
        kind = str(raw["decision_kind"])
        policy = str(raw["policy_path"])
        prob = float(raw["predicted_prob"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"calibration record missing or malformed required field: {exc}"
        raise CalibrationLogError(msg) from exc
    metadata_raw: object = raw.get("metadata") or {}
    if not isinstance(metadata_raw, dict):
        msg = "metadata must be a JSON object"
        raise CalibrationLogError(msg)
    metadata: dict[str, Any] = cast("dict[str, Any]", metadata_raw)
    decision_id_raw: object = raw.get("decision_id")
    decision_id = str(decision_id_raw) if decision_id_raw is not None else None
    return CalibrationRecord(
        timestamp=ts,
        decision_kind=kind,
        policy_path=policy,
        predicted_prob=prob,
        observed_outcome=outcome_raw,
        decision_id=decision_id,
        metadata=metadata,
    )


def log_decision(
    *,
    decision_kind: str,
    policy_path: str,
    predicted_prob: float,
    observed_outcome: bool,
    decision_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    log_path: Path | None = None,
    timestamp: float | None = None,
) -> CalibrationRecord:
    """Append a calibration record to the on-disk JSONL log.

    Args:
        decision_kind: Categorical decision label, e.g. ``"model_route"``.
        policy_path: Identifier of the policy that produced the probability.
        predicted_prob: Probability in ``[0, 1]``.
        observed_outcome: Eventual binary outcome (``True`` = win).
        decision_id: Optional join key for external logs.
        metadata: Optional auxiliary fields persisted verbatim.
        log_path: Override for the JSONL log path. Defaults to
            :data:`DEFAULT_LOG_PATH` relative to the current working directory.
        timestamp: Override timestamp; defaults to :func:`time.time`.

    Returns:
        The persisted :class:`CalibrationRecord`.

    Raises:
        CalibrationLogError: If validation fails.
    """
    record = CalibrationRecord(
        timestamp=time.time() if timestamp is None else timestamp,
        decision_kind=decision_kind,
        policy_path=policy_path,
        predicted_prob=predicted_prob,
        observed_outcome=observed_outcome,
        decision_id=decision_id,
        metadata=dict(metadata or {}),
    )
    path = log_path if log_path is not None else DEFAULT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _record_to_json(record)
    # Append-only; one JSON object per line; trailing newline preserved so
    # readers can rely on iteration by line.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write(os.linesep if os.linesep == "\n" else "\n")
    return record


def load_log(
    log_path: Path | None = None,
    *,
    since_seconds: float | None = None,
    decision_kind: str | None = None,
    now: float | None = None,
) -> list[CalibrationRecord]:
    """Read the calibration log from disk, applying optional filters.

    Args:
        log_path: Override path. Defaults to :data:`DEFAULT_LOG_PATH`.
        since_seconds: Drop records older than ``now - since_seconds``. When
            ``None`` (the default) all records are returned.
        decision_kind: Restrict to records with this ``decision_kind``.
        now: Override the reference time (defaults to :func:`time.time`).

    Returns:
        A list of records in file order. Missing or empty logs return ``[]``.

    Raises:
        CalibrationLogError: If any line is malformed.
    """
    path = log_path if log_path is not None else DEFAULT_LOG_PATH
    if not path.exists():
        return []
    cutoff: float | None = None
    if since_seconds is not None:
        ref = time.time() if now is None else now
        cutoff = ref - since_seconds
    out: list[CalibrationRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            record = _record_from_json(line)
            if cutoff is not None and record.timestamp < cutoff:
                continue
            if decision_kind is not None and record.decision_kind != decision_kind:
                continue
            out.append(record)
    return out


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def parse_duration(spec: str) -> float:
    """Parse a duration spec like ``"7d"`` or ``"30m"`` into seconds.

    Supported suffixes: ``s`` (seconds), ``m`` (minutes), ``h`` (hours),
    ``d`` (days), ``w`` (weeks). Case-insensitive.

    Args:
        spec: A non-empty duration spec.

    Returns:
        The duration in seconds as a float.

    Raises:
        ValueError: If ``spec`` is empty or malformed.
    """
    if not spec:
        msg = "duration spec must be non-empty"
        raise ValueError(msg)
    match = _DURATION_RE.match(spec)
    if match is None:
        msg = f"cannot parse duration {spec!r}; use e.g. '7d', '30m', '24h'"
        raise ValueError(msg)
    value = int(match.group(1))
    unit = match.group(2).lower()
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}[unit]
    return float(value * factor)


def _records_to_arrays(
    records: Iterable[CalibrationRecord],
) -> tuple[list[float], list[bool]]:
    """Split records into parallel ``predicted``/``observed`` lists."""
    preds: list[float] = []
    obs: list[bool] = []
    for r in records:
        preds.append(r.predicted_prob)
        obs.append(r.observed_outcome)
    return preds, obs


def compute_report(
    records: Sequence[CalibrationRecord],
    *,
    bin_count: int = _DEFAULT_BIN_COUNT,
    decision_kind: str | None = None,
    since: str | None = None,
) -> CalibrationReport:
    """Compute a :class:`CalibrationReport` from a sequence of records.

    Args:
        records: Calibration records to summarise.
        bin_count: Number of reliability buckets.
        decision_kind: Optional filter label persisted on the report.
        since: Optional duration spec persisted on the report.

    Returns:
        A :class:`CalibrationReport`. When ``records`` is empty, ``brier``
        and ``ece`` are ``None`` and ``buckets`` is empty - never raises.
    """
    if not records:
        return CalibrationReport(
            decisions=0,
            brier=None,
            ece=None,
            buckets=(),
            decision_kind=decision_kind,
            since=since,
        )
    preds, obs = _records_to_arrays(records)
    brier = compute_brier(preds, obs)
    ece = expected_calibration_error(preds, obs, bin_count=bin_count)
    buckets = reliability_diagram_data(preds, obs, bin_count=bin_count)
    return CalibrationReport(
        decisions=brier.sample_count,
        brier=brier.score,
        ece=ece,
        buckets=buckets,
        decision_kind=decision_kind,
        since=since,
    )


def iter_records(records: Iterable[CalibrationRecord]) -> Iterator[CalibrationRecord]:
    """Yield records lazily - used for streaming callers."""
    yield from records
