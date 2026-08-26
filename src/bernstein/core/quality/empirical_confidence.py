"""Empirical confidence from outcome history.

This module provides:

* :func:`confidence` - Sample-size-gated empirical confidence from outcome history.
* :func:`hoeffding_confidence_sequence` - Time-uniform confidence bounds based on
  Hoeffding's inequality for binary outcomes.

Records per-decision outcomes in an append-only SQLite table and exposes a
sample-size-gated confidence query. The query refuses to return a value when
the sample size is below a documented threshold; callers fall back to a
uniform prior or another signal of their choice.

The table is decoupled from any run-log so that outcome population can lag,
backfill, or be replayed without disturbing run-history semantics.

Public API:
    * ``record_outcome(agent_type, decision_key, outcome, ...)``
    * ``confidence(agent_type, decision_key, ...) -> Confidence``
    * ``ConfidenceQuery`` for ergonomic dependency injection.

Schema (single table, ``agent_outcomes``):
    id              INTEGER PRIMARY KEY AUTOINCREMENT
    agent_type      TEXT    NOT NULL
    decision_key    TEXT    NOT NULL
    outcome         INTEGER NOT NULL   -- 1 = correct, 0 = incorrect
    sampled_at      REAL    NOT NULL   -- POSIX seconds, UTC
    evidence_uri    TEXT                -- optional reference to a run or artefact

Indexes are created on ``(agent_type, decision_key)`` for fast aggregate
queries.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults and configuration
# ---------------------------------------------------------------------------


DEFAULT_MIN_SAMPLES: int = 5
"""Minimum sample count before :func:`confidence` returns a value.

Callers receive a :class:`Confidence` with ``value=None`` and
``insufficient_data=True`` below this threshold. The default of 5 is the
smallest sample size at which the binomial confidence interval narrows
enough to discriminate adjacent decision keys; tune via
``BERNSTEIN_CONFIDENCE_MIN_SAMPLES`` for stricter gating.
"""


DEFAULT_PRIOR: float = 0.5
"""Uniform prior returned by ``ConfidenceQuery.get_or_default`` when no
sufficient sample is available. Documented so routing decisions are
reproducible.
"""


_ENV_MIN_SAMPLES = "BERNSTEIN_CONFIDENCE_MIN_SAMPLES"
_ENV_DB_PATH = "BERNSTEIN_CONFIDENCE_DB"


def _xdg_data_home() -> Path:
    """Return the XDG_DATA_HOME path with the documented fallback."""
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".local" / "share"


def default_db_path() -> Path:
    """Resolved on-disk path for the empirical confidence SQLite file.

    Honours ``BERNSTEIN_CONFIDENCE_DB`` for test override, otherwise the
    XDG-conventional ``~/.local/share/bernstein/empirical-confidence.db``.
    """
    override = os.environ.get(_ENV_DB_PATH, "").strip()
    if override:
        return Path(override)
    return _xdg_data_home() / "bernstein" / "empirical-confidence.db"


def _resolve_min_samples(explicit: int | None) -> int:
    """Resolve the sample-size threshold from arg, env, or default."""
    if explicit is not None:
        return max(1, int(explicit))
    raw = os.environ.get(_ENV_MIN_SAMPLES, "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed >= 1:
                return parsed
        except ValueError:
            logger.warning("Ignoring invalid %s=%r", _ENV_MIN_SAMPLES, raw)
    return DEFAULT_MIN_SAMPLES


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Confidence:
    """Sample-size-gated confidence value.

    Attributes:
        value: Historical accuracy in ``[0.0, 1.0]`` if the sample is large
            enough; otherwise ``None``.
        samples: Number of recorded outcomes for the queried key.
        insufficient_data: ``True`` when ``samples < min_samples``.
        min_samples: Threshold used for this query.
    """

    value: float | None
    samples: int
    insufficient_data: bool
    min_samples: int

    def as_float(self, default: float = DEFAULT_PRIOR) -> float:
        """Return ``value`` if available, otherwise ``default``."""
        return self.value if self.value is not None else default

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict."""
        return {
            "value": None if self.value is None else round(self.value, 4),
            "samples": self.samples,
            "insufficient_data": self.insufficient_data,
            "min_samples": self.min_samples,
        }


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_type    TEXT    NOT NULL,
    decision_key  TEXT    NOT NULL,
    outcome       INTEGER NOT NULL CHECK (outcome IN (0, 1)),
    sampled_at    REAL    NOT NULL,
    evidence_uri  TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_outcomes_lookup
    ON agent_outcomes (agent_type, decision_key);
"""


class _OutcomeStore:
    """Thin SQLite wrapper around the ``agent_outcomes`` table.

    Connections are short-lived per operation. A module-level lock keeps
    schema migrations race-free; SQLite itself handles concurrent appends.
    """

    _migration_lock = threading.Lock()

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._ensure_parent()
        self._migrate()

    def _ensure_parent(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _migrate(self) -> None:
        with self._migration_lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
        try:
            # WAL keeps reads non-blocking against concurrent appends. Not
            # every SQLite build ships WAL; the fall-back is silent.
            with suppress(sqlite3.DatabaseError):
                conn.execute("PRAGMA journal_mode=WAL;")
            yield conn
        finally:
            conn.close()

    def append(
        self,
        *,
        agent_type: str,
        decision_key: str,
        outcome: int,
        sampled_at: float,
        evidence_uri: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_outcomes "
                "(agent_type, decision_key, outcome, sampled_at, evidence_uri) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_type, decision_key, outcome, sampled_at, evidence_uri),
            )

    def aggregate(self, *, agent_type: str, decision_key: str) -> tuple[int, float]:
        """Return ``(sample_count, mean_outcome)`` for the key."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(AVG(outcome), 0.0) "
                "FROM agent_outcomes "
                "WHERE agent_type = ? AND decision_key = ?",
                (agent_type, decision_key),
            ).fetchone()
        if row is None:
            return 0, 0.0
        return int(row[0]), float(row[1])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class ConfidenceQuery:
    """Read-side facade over the outcome ledger.

    Useful for dependency injection: callers that need ``get(agent, key)``
    can hold a single instance rather than the function-level API.
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        min_samples: int | None = None,
    ) -> None:
        self._db_path = db_path or default_db_path()
        self._store = _OutcomeStore(self._db_path)
        self._min_samples = _resolve_min_samples(min_samples)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def min_samples(self) -> int:
        return self._min_samples

    def record(
        self,
        agent_type: str,
        decision_key: str,
        outcome: bool | int,
        *,
        evidence_uri: str | None = None,
        sampled_at: float | None = None,
    ) -> None:
        """Append a single outcome row.

        ``outcome`` is coerced to ``1`` for truthy values and ``0`` otherwise
        so callers can pass either booleans or explicit ints.
        """
        _validate_key(agent_type, "agent_type")
        _validate_key(decision_key, "decision_key")
        ts = sampled_at if sampled_at is not None else time.time()
        self._store.append(
            agent_type=agent_type,
            decision_key=decision_key,
            outcome=1 if bool(outcome) else 0,
            sampled_at=ts,
            evidence_uri=evidence_uri,
        )

    def get(self, agent_type: str, decision_key: str) -> Confidence:
        """Return the sample-gated empirical confidence for the key."""
        _validate_key(agent_type, "agent_type")
        _validate_key(decision_key, "decision_key")
        samples, mean = self._store.aggregate(agent_type=agent_type, decision_key=decision_key)
        if samples < self._min_samples:
            return Confidence(
                value=None,
                samples=samples,
                insufficient_data=True,
                min_samples=self._min_samples,
            )
        return Confidence(
            value=mean,
            samples=samples,
            insufficient_data=False,
            min_samples=self._min_samples,
        )

    def get_or_default(
        self,
        agent_type: str,
        decision_key: str,
        *,
        default: float = DEFAULT_PRIOR,
    ) -> float:
        """Convenience: empirical value if available, else the prior."""
        return self.get(agent_type, decision_key).as_float(default=default)


def _validate_key(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        msg = f"{field_name} must be a non-empty string"
        raise ValueError(msg)


# Module-level default query, lazy so tests can override the DB path before
# the first call.
_default_query: ConfidenceQuery | None = None
_default_lock = threading.Lock()


def _get_default_query() -> ConfidenceQuery:
    global _default_query
    if _default_query is None:
        with _default_lock:
            if _default_query is None:
                _default_query = ConfidenceQuery()
    return _default_query


def reset_default_query() -> None:
    """Drop the cached module-level query. Intended for tests."""
    global _default_query
    with _default_lock:
        _default_query = None


def record_outcome(
    agent_type: str,
    decision_key: str,
    outcome: bool | int,
    *,
    evidence_uri: str | None = None,
    sampled_at: float | None = None,
) -> None:
    """Append a single outcome row via the default query."""
    _get_default_query().record(
        agent_type,
        decision_key,
        outcome,
        evidence_uri=evidence_uri,
        sampled_at=sampled_at,
    )


def confidence(agent_type: str, decision_key: str) -> Confidence:
    """Return sample-gated empirical confidence via the default query."""
    return _get_default_query().get(agent_type, decision_key)


# ---------------------------------------------------------------------------
# Hoeffding confidence sequence for binary outcomes
# ---------------------------------------------------------------------------


def hoeffding_confidence_sequence(k: int, n: int, alpha: float) -> tuple[float, float]:
    """Return Hoeffding time-uniform confidence bound for k successes in n trials.

    The Hoeffding bound is valid simultaneously at every sample size, so stopping
    when the lower bound exceeds the threshold is legitimate at any n.

    Args:
        k: Number of successes
        n: Total trials
        alpha: Error level (target false-admission rate)

    Returns:
        (lower_bound, upper_bound) for the true success probability, canonically
        rounded to 10 decimal places for byte-stable reproducibility.

    Raises:
        ValueError: If k > n, n < 0, or k < 0.
    """
    if n < 0:
        msg = f"n must be non-negative, got {n}"
        raise ValueError(msg)
    if k < 0:
        msg = f"k must be non-negative, got {k}"
        raise ValueError(msg)
    if k > n:
        msg = f"k cannot exceed n ({k} > {n})"
        raise ValueError(msg)

    if n == 0:
        return (0.0, 1.0)

    p_hat = k / n

    # Hoeffding bound: lower_bound = p_hat - sqrt(ln(2/delta)/(2*n))
    # where delta is the error level (alpha)
    # Compute ln(2/delta) using math.log, then canonical_round it before using
    import math

    ln_term = math.log(2.0 / alpha)
    ln_term_canonical = canonical_round(ln_term)

    bound = math.sqrt(ln_term_canonical / (2.0 * n))
    bound_canonical = canonical_round(bound)

    lower_bound = p_hat - bound_canonical
    upper_bound = p_hat + bound_canonical

    # Clamp to [0, 1]
    lower_clamped = max(0.0, lower_bound)
    upper_clamped = min(1.0, upper_bound)

    return (canonical_round(lower_clamped), canonical_round(upper_clamped))


def canonical_round(value: float, places: int = 10) -> float:
    """Round value to places decimals with round-half-to-even.

    This is a simplified copy of the canonical_round function from significance.py
    to avoid circular imports and keep this module dependency-free.
    """
    import math
    from decimal import ROUND_HALF_EVEN, Decimal

    if math.isnan(value) or math.isinf(value):
        msg = f"cannot canonically round non-finite value {value!r}"
        raise ValueError(msg)
    quantum = Decimal(1).scaleb(-places)
    quantised = Decimal(value).quantize(quantum, rounding=ROUND_HALF_EVEN)
    return float(quantised) + 0.0


__all__ = [
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_PRIOR",
    "Confidence",
    "ConfidenceQuery",
    "confidence",
    "default_db_path",
    "hoeffding_confidence_sequence",
    "record_outcome",
    "reset_default_query",
]
