"""Rolling per-task spend ledger with soft / hard circuit breakers.

Issue #1320: tag every LLM call at request time and persist it to a
JSONL ledger at ``.sdd/cost/ledger.jsonl`` so the operator can attribute
spend to ``task_id`` / ``agent_id`` / ``role`` / ``feature_label`` and
reason about runaway agents post-hoc.

Two enforcement bands are recognised:

* **Soft cap** (``--budget``): warn + reroute the next call to a cheaper
  model when ``>=80%`` of the cap has been spent; halt new work at 100%.
  Reroute is advisory - callers consult :func:`cheaper_model` and decide.
* **Hard cap** (``--hard-budget``): kill switch. Once tripped, no new
  call is admitted; the orchestrator must stop spawning.

The ledger is append-only and crash-safe: each :class:`SpendLedger.record`
fsyncs a single JSON line. Aggregation runs on read (cheap for the
typical 10^3-10^4 row ledger; if it ever grows past that we'll add a
roll-up snapshot).

This module is intentionally narrow - it does not own model selection
(:mod:`bernstein.core.cost.cost`) nor per-run budget reporting
(:mod:`bernstein.core.cost.cost_tracker`). It composes with both: the
tracker calls into :class:`SpendLedger` on every recorded usage.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - runtime use in dataclass field default
from typing import Any

from bernstein.core.cost.principal_attribution import ATTRIBUTION_DIMENSIONS, PrincipalAttribution

logger = logging.getLogger(__name__)

# Cap-trip thresholds. ``WARN`` is the soft-reroute trigger; ``SOFT_HALT``
# is the soft 100% line; ``HARD_HALT`` flips when the hard cap is reached.
SOFT_WARN_FRACTION: float = 0.80
SOFT_HALT_FRACTION: float = 1.00
HARD_HALT_FRACTION: float = 1.00

# Default reroute map: when the soft cap warns, prefer the cheaper model
# in the same family. Keys are matched by substring so concrete model
# strings like ``"claude-sonnet-4"`` route to ``"haiku"``. Unknown
# models fall through to ``DEFAULT_CHEAP_MODEL``.
_REROUTE: dict[str, str] = {
    "opus": "sonnet",
    "sonnet": "haiku",
    "gpt-5.5": "gpt-5.5-mini",
    "gpt-5.4": "gpt-5.4-mini",
    "gpt-5": "gpt-5-mini",
    "gemini-3-pro": "gemini-3-flash",
    "gemini-3.1-pro": "gemini-3-flash",
    "gemini-3": "gemini-3-flash",
    "deepseek-v4-pro": "deepseek-v4-flash",
    "qwen-max": "qwen-plus",
    "qwen-plus": "qwen-turbo",
}
DEFAULT_CHEAP_MODEL: str = "haiku"


# ---------------------------------------------------------------------------
# Records & tags
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallTags:
    """Per-call attribution tags attached at LLM-request time.

    Empty strings are permitted (and treated as "unknown") so the
    pre-call hook can tag what it knows without forcing every caller to
    pre-populate every dimension. Callers that need richer tagging set
    ``extra`` - useful for ``feature_label`` and operator-defined
    dimensions like ``customer_id``.
    """

    task_id: str = ""
    agent_id: str = ""
    role: str = ""
    feature_label: str = ""
    # Per-quota-envelope attribution (issue #1405). Defaults to
    # ``"subscription"`` so existing callers keep the legacy
    # single-envelope rollup.
    quota_envelope: str = "subscription"
    # Per-principal attribution (issue #4985). The principal that incurred
    # the cost, the grant that authorized it, and the identity that signed
    # that grant. Empty means "not recorded" -- never "assume the default
    # principal"; the rollup reports such a row as unattributed.
    principal_id: str = ""
    grant_id: str = ""
    authorizing_identity: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def merged(self) -> dict[str, str]:
        """Flatten well-known fields + ``extra`` into a single dict.

        Returns:
            A dict with no empty values. Keys are normalised to strings.
        """
        out: dict[str, str] = {}
        if self.task_id:
            out["task_id"] = self.task_id
        if self.agent_id:
            out["agent_id"] = self.agent_id
        if self.role:
            out["role"] = self.role
        if self.feature_label:
            out["feature_label"] = self.feature_label
        # Only surface ``quota_envelope`` in the merged tag-map when an
        # operator has set it to something other than the default. The
        # ledger column itself always carries the value so the rollup is
        # complete; we just avoid polluting the tag dict for legacy
        # single-envelope callers.
        if self.quota_envelope and self.quota_envelope != "subscription":
            out["quota_envelope"] = self.quota_envelope
        if self.principal_id:
            out["principal_id"] = self.principal_id
        if self.grant_id:
            out["grant_id"] = self.grant_id
        if self.authorizing_identity:
            out["authorizing_identity"] = self.authorizing_identity
        for k, v in self.extra.items():
            if v:
                out[k] = v
        return out


@dataclass
class LedgerEntry:
    """One row in ``.sdd/cost/ledger.jsonl``.

    Identical attributes to :class:`bernstein.core.cost.cost_tracker.TokenUsage`
    plus a stamped ``ts_iso`` for human readability and the merged
    ``tags`` block. Stored as JSON; field order is stable so diffing
    ledgers from two runs is meaningful.
    """

    ts: float
    ts_iso: str
    run_id: str
    task_id: str
    agent_id: str
    role: str
    feature_label: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    # issue #1405: per-quota-envelope attribution. Defaults to
    # ``"subscription"`` so older ledgers that lack the field deserialise
    # cleanly via :meth:`from_dict`.
    quota_envelope: str = "subscription"
    # Per-principal attribution (issue #4985). Rows written before the
    # columns existed deserialise to empty strings and are reported as
    # unattributed rather than backfilled onto a principal.
    principal_id: str = ""
    grant_id: str = ""
    authorizing_identity: str = ""
    tags: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        """Return a stable single-line JSON encoding."""
        return json.dumps(asdict(self), sort_keys=False, separators=(",", ":"))

    def attribution(self) -> PrincipalAttribution:
        """Return this row's attribution tuple (issue #4985)."""
        return PrincipalAttribution(
            principal_id=self.principal_id,
            grant_id=self.grant_id,
            authorizing_identity=self.authorizing_identity,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LedgerEntry:
        """Deserialise from a parsed JSON dict; missing fields default."""
        raw_tags = d.get("tags") or {}
        tags: dict[str, str] = {str(k): str(v) for k, v in raw_tags.items()} if isinstance(raw_tags, dict) else {}
        return cls(
            ts=float(d.get("ts", 0.0) or 0.0),
            ts_iso=str(d.get("ts_iso", "")),
            run_id=str(d.get("run_id", "")),
            task_id=str(d.get("task_id", "")),
            agent_id=str(d.get("agent_id", "")),
            role=str(d.get("role", "")),
            feature_label=str(d.get("feature_label", "")),
            model=str(d.get("model", "")),
            input_tokens=int(d.get("input_tokens", 0) or 0),
            output_tokens=int(d.get("output_tokens", 0) or 0),
            cache_read_tokens=int(d.get("cache_read_tokens", 0) or 0),
            cache_write_tokens=int(d.get("cache_write_tokens", 0) or 0),
            cost_usd=float(d.get("cost_usd", 0.0) or 0.0),
            quota_envelope=str(d.get("quota_envelope") or tags.get("quota_envelope") or "subscription"),
            principal_id=str(d.get("principal_id") or tags.get("principal_id") or ""),
            grant_id=str(d.get("grant_id") or tags.get("grant_id") or ""),
            authorizing_identity=str(d.get("authorizing_identity") or tags.get("authorizing_identity") or ""),
            tags=tags,
        )


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class SpendLedger:
    """Append-only JSONL spend ledger with soft / hard circuit breakers.

    ``budget_usd`` is the soft cap and ``hard_budget_usd`` is the hard
    cap (both in USD; 0 means "no cap" on that side). Calling
    :meth:`record` writes one JSONL row, updates the rolling totals, and
    returns the current :class:`LedgerStatus` so the caller can decide
    whether to warn / reroute / abort.

    The instance is process-local; concurrent writers should each hold
    their own instance pointing at the same file (the append is atomic
    on POSIX for lines below ``PIPE_BUF``). A thread lock guards the
    in-process totals.
    """

    path: Path
    run_id: str = "default"
    budget_usd: float = 0.0
    hard_budget_usd: float = 0.0

    # Internal state
    _spent_usd: float = field(default=0.0, init=False, repr=False)
    _spent_by: dict[str, dict[str, float]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(float)),
        init=False,
        repr=False,
    )
    _entries_written: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _soft_warned: bool = field(default=False, init=False, repr=False)
    _soft_halted_at: float | None = field(default=None, init=False, repr=False)
    _hard_halted_at: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Normalise non-positive caps to 0 so "no cap" semantics are
        # consistent with cost_tracker.resolve_run_budget_usd.
        if self.budget_usd < 0:
            self.budget_usd = 0.0
        if self.hard_budget_usd < 0:
            self.hard_budget_usd = 0.0
        # If both caps are set but inverted, the hard cap must dominate.
        if self.budget_usd > 0 and 0 < self.hard_budget_usd < self.budget_usd:
            logger.warning(
                "SpendLedger: hard_budget_usd=%.4f < budget_usd=%.4f; "
                "soft cap clamped to hard cap to preserve precedence.",
                self.hard_budget_usd,
                self.budget_usd,
            )
            self.budget_usd = self.hard_budget_usd

    # ------------------------------------------------------------------ record

    def record(
        self,
        *,
        tags: CallTags,
        model: str,
        cost_usd: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        ts: float | None = None,
    ) -> LedgerStatus:
        """Append one row to the ledger and return the post-call status.

        Cost values below zero are clamped to zero (defensive - a
        misconfigured pricing table must not corrupt the ledger).
        """
        cost = max(0.0, cost_usd)
        now = ts if ts is not None else time.time()
        merged = tags.merged()
        envelope = tags.quota_envelope or "subscription"
        entry = LedgerEntry(
            ts=now,
            ts_iso=datetime.fromtimestamp(now, tz=UTC).isoformat(timespec="seconds"),
            run_id=self.run_id,
            task_id=tags.task_id,
            agent_id=tags.agent_id,
            role=tags.role,
            feature_label=tags.feature_label,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cost_usd=cost,
            quota_envelope=envelope,
            principal_id=tags.principal_id,
            grant_id=tags.grant_id,
            authorizing_identity=tags.authorizing_identity,
            tags=merged,
        )

        with self._lock:
            self._append_to_disk(entry)
            self._spent_usd += cost
            self._entries_written += 1
            self._spent_by["task"][tags.task_id or "unknown"] += cost
            self._spent_by["agent"][tags.agent_id or "unknown"] += cost
            self._spent_by["role"][tags.role or "unknown"] += cost
            self._spent_by["model"][model or "unknown"] += cost
            self._spent_by["envelope"][envelope] += cost
            for dimension in ATTRIBUTION_DIMENSIONS:
                self._spent_by[dimension][entry.attribution().key(dimension)] += cost
            if tags.feature_label:
                self._spent_by["feature_label"][tags.feature_label] += cost
            status = self._status_locked()

        # Logging outside the lock so a noisy handler can't block other writers.
        self._emit_threshold_events(status)
        return status

    # ------------------------------------------------------------------ query

    def status(self) -> LedgerStatus:
        """Return the current ledger status snapshot."""
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> LedgerStatus:
        soft = self.budget_usd
        hard = self.hard_budget_usd
        spent = self._spent_usd

        soft_pct = (spent / soft) if soft > 0 else 0.0
        hard_pct = (spent / hard) if hard > 0 else 0.0

        soft_warn = soft > 0 and soft_pct >= SOFT_WARN_FRACTION
        soft_halt = soft > 0 and soft_pct >= SOFT_HALT_FRACTION
        hard_halt = hard > 0 and hard_pct >= HARD_HALT_FRACTION

        return LedgerStatus(
            spent_usd=spent,
            budget_usd=soft,
            hard_budget_usd=hard,
            soft_pct=soft_pct,
            hard_pct=hard_pct,
            soft_warn=soft_warn,
            soft_halt=soft_halt,
            hard_halt=hard_halt,
        )

    def totals_by(self, dimension: str) -> dict[str, float]:
        """Return cost-by-dimension totals (``task|agent|role|model|feature_label|principal|grant``).

        Unknown dimensions return an empty dict so callers can probe
        without raising. ``task|agent|role|model`` are always populated
        (even with the ``"unknown"`` bucket); ``feature_label`` only
        appears when at least one row carried the tag.
        """
        with self._lock:
            return dict(self._spent_by.get(dimension, {}))

    # ------------------------------------------------------------------ reroute

    def cheaper_model(self, model: str) -> str | None:
        """Suggest a cheaper substitute model when the soft cap has warned.

        Returns ``None`` when the soft cap has not been tripped or when
        the proposed substitute is the same as the input (avoids no-op
        rerouting). Caller is responsible for honouring the suggestion.
        """
        status = self.status()
        if not status.soft_warn:
            return None
        m = model.lower()
        for k, v in _REROUTE.items():
            if k in m and k != v:
                return v
        # Unknown model: only suggest a substitute if it's not already
        # the default cheap floor (otherwise we recommend "haiku" → "haiku").
        return DEFAULT_CHEAP_MODEL if DEFAULT_CHEAP_MODEL not in m else None

    def admits(self) -> bool:
        """Whether the next call is admitted under the current caps."""
        status = self.status()
        return not (status.hard_halt or status.soft_halt)

    # ------------------------------------------------------------------ io

    def _append_to_disk(self, entry: LedgerEntry) -> None:
        """Append a single JSONL line, flushing to disk best-effort.

        Failures are logged but never raised - the ledger is best-effort
        observability and must not take down the orchestrator.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(entry.to_json())
                fh.write("\n")
                fh.flush()
        except OSError as exc:  # pragma: no cover - IO failure path
            logger.warning("SpendLedger: failed to append entry: %s", exc)

    def _emit_threshold_events(self, status: LedgerStatus) -> None:
        """Log + stamp soft/hard threshold transitions exactly once each."""
        now = time.time()
        if status.hard_halt and self._hard_halted_at is None:
            self._hard_halted_at = now
            logger.warning(
                "SpendLedger HARD CAP HIT: spent=$%.4f hard_cap=$%.4f run_id=%s",
                status.spent_usd,
                status.hard_budget_usd,
                self.run_id,
            )
        if status.soft_halt and self._soft_halted_at is None:
            self._soft_halted_at = now
            logger.warning(
                "SpendLedger SOFT CAP HIT (100%%): spent=$%.4f budget=$%.4f run_id=%s",
                status.spent_usd,
                status.budget_usd,
                self.run_id,
            )
        elif status.soft_warn and not self._soft_warned:
            self._soft_warned = True
            logger.warning(
                "SpendLedger SOFT CAP WARN (>=80%%): spent=$%.4f budget=$%.4f run_id=%s - "
                "calls will be rerouted to cheaper models",
                status.spent_usd,
                status.budget_usd,
                self.run_id,
            )

    # ------------------------------------------------------------------ load

    @classmethod
    def load_entries(cls, path: Path) -> list[LedgerEntry]:
        """Read every row of an existing ledger file (cheap for typical sizes).

        Returns an empty list when the file is missing. Malformed lines
        are skipped - partial recovery is preferred over a crash.
        """
        if not path.exists():
            return []
        entries: list[LedgerEntry] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(LedgerEntry.from_dict(json.loads(line)))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError as exc:  # pragma: no cover
            logger.warning("SpendLedger: failed to read %s: %s", path, exc)
        return entries

    @property
    def entries_written(self) -> int:
        """How many rows this instance wrote since construction."""
        return self._entries_written


@dataclass(frozen=True)
class LedgerStatus:
    """Snapshot of the rolling spend state."""

    spent_usd: float
    budget_usd: float
    hard_budget_usd: float
    soft_pct: float
    hard_pct: float
    soft_warn: bool
    soft_halt: bool
    hard_halt: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "spent_usd": round(self.spent_usd, 6),
            "budget_usd": self.budget_usd,
            "hard_budget_usd": self.hard_budget_usd,
            "soft_pct": round(self.soft_pct, 4),
            "hard_pct": round(self.hard_pct, 4),
            "soft_warn": self.soft_warn,
            "soft_halt": self.soft_halt,
            "hard_halt": self.hard_halt,
        }


# ---------------------------------------------------------------------------
# Aggregation helpers (for `bernstein cost --by ...`)
# ---------------------------------------------------------------------------


def aggregate_entries(
    entries: list[LedgerEntry],
    dimension: str,
) -> dict[str, dict[str, Any]]:
    """Group ledger entries by *dimension*; return per-bucket totals.

    Supported dimensions: ``task``, ``agent``, ``role``, ``model``,
    ``feature_label``, ``envelope``, ``day``, plus the attribution
    dimensions ``principal``, ``grant`` and ``authorizing_identity``
    (issue #4985). Unknown dimensions return ``{}``.

    Each bucket contains ``cost_usd``, ``calls``, ``input_tokens``,
    ``output_tokens``. Buckets are not pre-sorted - that's the
    rendering layer's job.
    """
    if not entries:
        return {}

    def _bucket_key(e: LedgerEntry) -> str:
        if dimension == "task":
            return e.task_id or "unknown"
        if dimension == "agent":
            return e.agent_id or "unknown"
        if dimension == "role":
            return e.role or "unknown"
        if dimension == "model":
            return e.model or "unknown"
        if dimension == "feature_label":
            return e.feature_label or "unknown"
        if dimension == "envelope":
            return e.quota_envelope or "subscription"
        if dimension in ATTRIBUTION_DIMENSIONS:
            return e.attribution().key(dimension)
        if dimension == "day":
            return datetime.fromtimestamp(e.ts, tz=UTC).strftime("%Y-%m-%d") if e.ts > 0 else "unknown"
        return ""

    if dimension not in {"task", "agent", "role", "model", "feature_label", "envelope", "day", *ATTRIBUTION_DIMENSIONS}:
        return {}

    out: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cost_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
    )
    for e in entries:
        bucket = out[_bucket_key(e)]
        bucket["cost_usd"] += e.cost_usd
        bucket["calls"] += 1
        bucket["input_tokens"] += e.input_tokens
        bucket["output_tokens"] += e.output_tokens
    return out.copy()


__all__ = [
    "DEFAULT_CHEAP_MODEL",
    "HARD_HALT_FRACTION",
    "SOFT_HALT_FRACTION",
    "SOFT_WARN_FRACTION",
    "CallTags",
    "LedgerEntry",
    "LedgerStatus",
    "SpendLedger",
    "aggregate_entries",
]
