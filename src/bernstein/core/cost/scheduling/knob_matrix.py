"""Versioned, hash-pinned dispatch knob matrix + pure resolver (#2519).

The cost-aware dispatch policy (:mod:`bernstein.core.cost.scheduling.policy`)
admits or halts a spawn from a USD projection over a hash-pinned price table,
but the projection was keyed by model name only. The per-call knobs that
actually move the price of an identical task -- the reasoning ``effort`` level,
the processing ``lane`` (interactive / priority / batch, with distinct rate
multipliers), and the prompt-cache ``strategy`` (none / reuse / warm-up) -- were
scattered and unpinned, so two runs of the same plan could execute with
different effective knobs, pay different amounts, and produce receipts that
verify equally well.

This module closes that gap the same way the price table did:

* :class:`KnobMatrix` is an immutable ``model -> ModelKnobs`` map plus an
  ``as_of`` date and a monotonic ``revision``; its :meth:`~KnobMatrix.content_hash`
  is a canonical-JSON SHA-256 pinning exactly which knob economics a decision
  used. Its defaults derive from the existing single sources of truth: the
  cache economics come from :data:`bernstein.core.cost.model_prices.MODEL_COSTS_PER_1M_TOKENS`
  rows (a model with a ``cache_read`` rate offers ``reuse``; one with a
  ``cache_write`` rate offers ``warm_up``), and the fact that batch and
  cache-window lanes exist at all derives from
  :data:`bernstein.adapters._contract.BATCH_DISPATCH_CAPABILITY_MATRIX` and
  :data:`bernstein.adapters._contract.CACHE_WINDOW_CAPABILITY_MATRIX`.
* :func:`load_knob_matrix` builds a matrix from validated config, so an operator
  overrides the shipped defaults without a code change (same contract as
  :func:`bernstein.core.cost.scheduling.price_table.load_price_table`).
* :func:`resolve_knob_selection` is a *pure* resolver -- no clock, no filesystem,
  no network -- that, given a :class:`DispatchCandidate` and the pinned matrix,
  returns a sealed :class:`KnobSelection`. Its selection hash folds into the
  decision hash, so the knob choice is part of the decision identity, not a
  side effect; a model absent from the matrix resolves to an explicit default
  carrying ``resolved=False`` and a machine-readable reason (never a silent
  fallback), and its ``rate_multiplier`` is ``1.0`` so the admit/halt outcome is
  unchanged from an unpriced model's current behaviour.
* :func:`knob_matrix_staleness` mirrors ``price_table_staleness`` for a
  non-blocking ``doctor`` advisory.

Determinism and tie-breaks (AC1). Every branch below is a pure function of the
candidate and the pinned matrix:

* **effort** -- the candidate's ``requested_effort`` is honoured when it is one
  of the model's declared levels; otherwise the resolver falls back to the
  model's declared ``default_effort`` and records the fallback reason. An empty
  request also uses the default.
* **lane** -- ``batch`` is chosen only when the candidate is ``batch_eligible``,
  the resolved ``adapter`` is batch-capable
  (:func:`bernstein.adapters._contract.batch_dispatch_capability` is ``NATIVE``),
  and the model declares a batch lane; otherwise the lane is ``interactive``.
  The batch lane carries the lowest multiplier, so choosing it can only lower a
  projection -- a lane upgrade never silently raises spend.
* **cache strategy** -- ``warm_up`` requires the adapter to be cache-window
  capable, the model to declare ``warm_up``, and the candidate to request it;
  ``reuse`` requires the model to declare ``reuse`` and the candidate to request
  ``reuse`` or ``warm_up``; otherwise ``none``.
* **rate_multiplier** -- the resolved lane's multiplier, the single headline
  factor applied to the candidate projection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from bernstein.adapters._contract import (
    BatchDispatchCapability,
    CacheWindowCapability,
    batch_dispatch_capability,
    cache_window_capability,
)
from bernstein.core.cost.model_prices import MODEL_COSTS_PER_1M_TOKENS
from bernstein.core.cost.scheduling.policy import DispatchCandidate, KnobSelection

#: ``as_of`` date shipped with :data:`DEFAULT_KNOB_MATRIX`. Mirrors the price
#: table's ``as_of`` -- the knob economics are derived from the same rows.
DEFAULT_KNOB_MATRIX_AS_OF = "2026-05-05"

#: Default window after which a knob matrix is advised stale (mirrors the price
#: table's staleness window; lane economics drift with provider pricing).
DEFAULT_STALENESS_WINDOW_DAYS = 90

#: The declared reasoning-effort ladder every priced model supports by default,
#: lowest to highest. Operators re-declare per model via config.
DEFAULT_EFFORT_LEVELS = ("low", "medium", "high", "max")

#: The default effort a candidate resolves to when it requests none, or requests
#: a level the model does not declare. A stable middle rung, never the extreme.
DEFAULT_EFFORT = "medium"

#: Processing lanes and their USD rate multipliers relative to interactive
#: list price. ``batch`` reflects the ~50% non-interactive discount the model
#: pricing comments document; ``priority`` a premium express lane. The *set* of
#: lanes exists because the adapter contract declares a batch surface
#: (:data:`bernstein.adapters._contract.BATCH_DISPATCH_CAPABILITY_MATRIX`);
#: whether a candidate may *select* the batch lane is gated per-adapter by the
#: resolver.
LANE_INTERACTIVE = "interactive"
LANE_PRIORITY = "priority"
LANE_BATCH = "batch"
LANE_MULTIPLIERS: dict[str, float] = {
    LANE_INTERACTIVE: 1.0,
    LANE_PRIORITY: 2.0,
    LANE_BATCH: 0.5,
}

#: Cache strategies. ``none`` is always available; ``reuse`` / ``warm_up`` are
#: declared per model only when the price row prices cache reads / writes, and
#: the resolver additionally gates ``warm_up`` on adapter cache-window
#: capability (:data:`bernstein.adapters._contract.CACHE_WINDOW_CAPABILITY_MATRIX`).
CACHE_NONE = "none"
CACHE_REUSE = "reuse"
CACHE_WARM_UP = "warm_up"

#: Machine-readable resolver reasons (AC: never a silent fallback).
REASON_RESOLVED = "resolved"
REASON_MODEL_NOT_IN_MATRIX = "model_not_in_matrix"
REASON_EFFORT_FALLBACK = "effort_not_declared_default_applied"


@dataclass(frozen=True)
class ModelKnobs:
    """The declared knob economics for one model.

    Attributes:
        effort_levels: Ordered, declared reasoning-effort levels (lowest first).
        default_effort: The level a candidate resolves to when it requests an
            undeclared or empty effort.
        lanes: ``lane -> USD rate multiplier`` (relative to interactive list
            price). ``interactive`` is always present with multiplier ``1.0``.
        cache_strategies: ``strategy -> relative input-token cost fraction``.
            ``none`` is always present at ``1.0``; ``reuse`` / ``warm_up`` are
            present only when the price row prices cache reads / writes.
    """

    effort_levels: tuple[str, ...]
    default_effort: str
    lanes: dict[str, float]
    cache_strategies: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "effort_levels": list(self.effort_levels),
            "default_effort": self.default_effort,
            "lanes": {name: round(mult, 6) for name, mult in self.lanes.items()},
            "cache_strategies": {name: round(frac, 6) for name, frac in self.cache_strategies.items()},
        }


@dataclass(frozen=True)
class KnobMatrix:
    """Immutable, content-addressed dispatch knob matrix.

    Attributes:
        models: Map of model key (matched case-insensitively as a substring,
            longest key first, exactly like the price table) to its
            :class:`ModelKnobs`.
        as_of: ISO date (``YYYY-MM-DD``) the economics were captured.
        revision: Monotonic revision counter, bumped on any change.
    """

    models: dict[str, ModelKnobs]
    as_of: str = DEFAULT_KNOB_MATRIX_AS_OF
    revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "revision": self.revision,
            "models": {name: knobs.to_dict() for name, knobs in self.models.items()},
        }

    def _canonical_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def content_hash(self) -> str:
        """``sha256:`` digest pinning exactly these knob economics + metadata."""
        return "sha256:" + hashlib.sha256(self._canonical_bytes()).hexdigest()

    def knobs_for(self, model: str) -> ModelKnobs | None:
        """Return the :class:`ModelKnobs` for *model*, longest key first.

        An unknown model returns ``None`` so the resolver can emit an explicit
        default selection rather than guessing -- identical fallback semantics
        to the price table's unknown-model path.
        """
        model_lower = model.lower()
        for key in sorted(self.models, key=len, reverse=True):
            if key.lower() in model_lower:
                return self.models[key]
        return None


def _default_model_knobs(pricing: Mapping[str, Any]) -> ModelKnobs:
    """Build one model's knobs from its leaf pricing row.

    Cache strategies derive from the row: a model that prices ``cache_read``
    offers ``reuse``; one that prices ``cache_write`` offers ``warm_up``. The
    per-strategy fraction is the cache rate over the input rate (rounded), the
    token economics the projection would apply.
    """
    input_rate = float(pricing.get("input", 0.0) or 0.0)
    cache_read = float(pricing.get("cache_read") or 0.0)
    cache_write = float(pricing.get("cache_write") or 0.0)

    cache_strategies: dict[str, float] = {CACHE_NONE: 1.0}
    if cache_read > 0.0:
        cache_strategies[CACHE_REUSE] = round(cache_read / input_rate, 6) if input_rate > 0.0 else 1.0
    if cache_write > 0.0:
        cache_strategies[CACHE_WARM_UP] = round(cache_write / input_rate, 6) if input_rate > 0.0 else 1.0

    return ModelKnobs(
        effort_levels=DEFAULT_EFFORT_LEVELS,
        default_effort=DEFAULT_EFFORT,
        lanes=LANE_MULTIPLIERS.copy(),
        cache_strategies=cache_strategies,
    )


def _default_models() -> dict[str, ModelKnobs]:
    """Build the shipped model knob map from the leaf pricing table."""
    return {key: _default_model_knobs(pricing) for key, pricing in MODEL_COSTS_PER_1M_TOKENS.items()}


#: Shipped default knob matrix, derived from the same rows the price table and
#: the adapter capability maps are. Hash-pinned via
#: :meth:`KnobMatrix.content_hash`.
DEFAULT_KNOB_MATRIX = KnobMatrix(models=_default_models(), as_of=DEFAULT_KNOB_MATRIX_AS_OF, revision=1)


def _coerce_knobs(raw: Mapping[str, Any], *, key: str) -> ModelKnobs:
    """Validate one config knob row into a :class:`ModelKnobs`.

    Raises:
        ValueError: A declared multiplier / fraction is non-numeric or negative,
            or the effort ladder is empty -- a misconfiguration that must fail
            loudly rather than corrupt every downstream dispatch fingerprint.
    """
    levels_raw = raw.get("effort_levels") or list(DEFAULT_EFFORT_LEVELS)
    effort_levels = tuple(str(level) for level in levels_raw)
    if not effort_levels:
        raise ValueError(f"knobs for model {key!r} declare an empty effort ladder")
    default_effort = str(raw.get("default_effort") or effort_levels[len(effort_levels) // 2])
    if default_effort not in effort_levels:
        raise ValueError(f"knobs for model {key!r} default_effort {default_effort!r} not in declared levels")

    def _numeric_map(field: str, fallback: dict[str, float], *, always: dict[str, float]) -> dict[str, float]:
        rows = raw.get(field)
        if not isinstance(rows, Mapping):
            return fallback.copy()
        out: dict[str, float] = always.copy()
        for name, value in rows.items():
            try:
                num = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"knobs for model {key!r} {field}[{name!r}] is non-numeric: {value!r}") from exc
            if num < 0:
                raise ValueError(f"knobs for model {key!r} {field}[{name!r}] is negative: {num}")
            out[str(name)] = num
        return out

    lanes = _numeric_map("lanes", LANE_MULTIPLIERS.copy(), always={LANE_INTERACTIVE: 1.0})
    cache_strategies = _numeric_map("cache_strategies", {CACHE_NONE: 1.0}, always={CACHE_NONE: 1.0})
    return ModelKnobs(
        effort_levels=effort_levels,
        default_effort=default_effort,
        lanes=lanes,
        cache_strategies=cache_strategies,
    )


def load_knob_matrix(
    models: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    as_of: str | None = None,
    revision: int = 0,
    base: KnobMatrix | None = None,
) -> KnobMatrix:
    """Build a :class:`KnobMatrix` from validated config knob rows.

    When *models* is ``None`` or empty the shipped :data:`DEFAULT_KNOB_MATRIX`
    (or *base*) is returned unchanged. Otherwise the config rows override /
    extend *base* (the shipped defaults by default), so an operator names only
    the models they want to re-declare.

    Raises:
        ValueError: A declared multiplier / fraction is non-numeric or negative,
            an effort ladder is empty, or a default effort is not declared.
    """
    base_matrix = base if base is not None else DEFAULT_KNOB_MATRIX
    if not models:
        return base_matrix
    merged: dict[str, ModelKnobs] = base_matrix.models.copy()
    for key, raw in models.items():
        merged[key] = _coerce_knobs(raw, key=key)
    return KnobMatrix(models=merged, as_of=as_of or base_matrix.as_of, revision=revision)


@dataclass(frozen=True, slots=True)
class StalenessAdvisory:
    """Result of a knob-matrix staleness check (drives the doctor advisory)."""

    stale: bool
    age_days: int
    as_of: str
    message: str


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def knob_matrix_staleness(
    matrix: KnobMatrix,
    *,
    now_iso: str,
    max_age_days: int = DEFAULT_STALENESS_WINDOW_DAYS,
) -> StalenessAdvisory:
    """Return whether *matrix* is older than ``max_age_days`` as of ``now_iso``.

    A malformed ``as_of`` is treated as stale (fail-visible). The advisory never
    raises and never blocks -- it is a doctor hint, mirroring
    :func:`bernstein.core.cost.scheduling.price_table.price_table_staleness`.
    """
    now = _parse_iso_date(now_iso)
    captured = _parse_iso_date(matrix.as_of)
    if now is None:
        return StalenessAdvisory(
            stale=False,
            age_days=0,
            as_of=matrix.as_of,
            message=f"knob matrix as_of={matrix.as_of!r}: cannot evaluate staleness (bad now {now_iso!r})",
        )
    if captured is None:
        return StalenessAdvisory(
            stale=True,
            age_days=max_age_days + 1,
            as_of=matrix.as_of,
            message=f"knob matrix as_of={matrix.as_of!r} is not a valid ISO date; treat economics as stale",
        )
    age_days = (now - captured).days
    stale = age_days > max_age_days
    if stale:
        message = (
            f"knob matrix as_of={matrix.as_of} is {age_days}d old (> {max_age_days}d); "
            "lane / cache economics may have drifted -- refresh cost_policy.knobs or the shipped matrix"
        )
    else:
        message = f"knob matrix as_of={matrix.as_of} is {age_days}d old (<= {max_age_days}d)"
    return StalenessAdvisory(stale=stale, age_days=age_days, as_of=matrix.as_of, message=message)


def _resolve_effort(knobs: ModelKnobs, requested: str) -> tuple[str, bool]:
    """Return ``(effort, fell_back)`` for a candidate's requested effort."""
    if requested and requested in knobs.effort_levels:
        return requested, False
    return knobs.default_effort, bool(requested)


def _resolve_lane(knobs: ModelKnobs, *, batch_eligible: bool, adapter: str) -> str:
    """Return the resolved processing lane (batch only when fully capable)."""
    if (
        batch_eligible
        and LANE_BATCH in knobs.lanes
        and batch_dispatch_capability(adapter) is BatchDispatchCapability.NATIVE
    ):
        return LANE_BATCH
    return LANE_INTERACTIVE


def _resolve_cache(knobs: ModelKnobs, *, requested: str, adapter: str) -> str:
    """Return the resolved cache strategy (warm-up gated on adapter capability)."""
    cache_capable = cache_window_capability(adapter) is CacheWindowCapability.SUPPORTED
    if requested == CACHE_WARM_UP and CACHE_WARM_UP in knobs.cache_strategies and cache_capable:
        return CACHE_WARM_UP
    if requested in (CACHE_REUSE, CACHE_WARM_UP) and CACHE_REUSE in knobs.cache_strategies:
        return CACHE_REUSE
    return CACHE_NONE


def resolve_knob_selection(*, candidate: DispatchCandidate, matrix: KnobMatrix) -> KnobSelection:
    """Resolve the sealed :class:`KnobSelection` for *candidate* (pure, AC1).

    Deterministic: no clock, no filesystem, no network -- the effort, lane, and
    cache strategy are pure functions of the candidate and the pinned *matrix*
    (plus the declared, never-probed adapter capability maps). Two operators
    with the same candidate and matrix derive a byte-identical sealed selection.

    A model absent from *matrix* resolves to an explicit default carrying
    ``resolved=False`` and :data:`REASON_MODEL_NOT_IN_MATRIX`, with
    ``rate_multiplier == 1.0`` so the admit/halt outcome is unchanged from an
    unpriced model's current behaviour (never a silent fallback).
    """
    matrix_hash = matrix.content_hash()
    knobs = matrix.knobs_for(candidate.model)
    if knobs is None:
        return KnobSelection(
            model=candidate.model,
            effort="",
            lane=LANE_INTERACTIVE,
            cache_strategy=CACHE_NONE,
            rate_multiplier=1.0,
            resolved=False,
            reason=REASON_MODEL_NOT_IN_MATRIX,
            matrix_hash=matrix_hash,
        ).sealed()

    effort, fell_back = _resolve_effort(knobs, candidate.requested_effort)
    lane = _resolve_lane(knobs, batch_eligible=candidate.batch_eligible, adapter=candidate.adapter)
    cache_strategy = _resolve_cache(knobs, requested=candidate.requested_cache, adapter=candidate.adapter)
    rate_multiplier = knobs.lanes.get(lane, 1.0)
    reason = REASON_EFFORT_FALLBACK if fell_back else REASON_RESOLVED

    return KnobSelection(
        model=candidate.model,
        effort=effort,
        lane=lane,
        cache_strategy=cache_strategy,
        rate_multiplier=rate_multiplier,
        resolved=True,
        reason=reason,
        matrix_hash=matrix_hash,
    ).sealed()


__all__ = [
    "CACHE_NONE",
    "CACHE_REUSE",
    "CACHE_WARM_UP",
    "DEFAULT_EFFORT",
    "DEFAULT_EFFORT_LEVELS",
    "DEFAULT_KNOB_MATRIX",
    "DEFAULT_KNOB_MATRIX_AS_OF",
    "DEFAULT_STALENESS_WINDOW_DAYS",
    "LANE_BATCH",
    "LANE_INTERACTIVE",
    "LANE_MULTIPLIERS",
    "LANE_PRIORITY",
    "REASON_EFFORT_FALLBACK",
    "REASON_MODEL_NOT_IN_MATRIX",
    "REASON_RESOLVED",
    "KnobMatrix",
    "KnobSelection",
    "ModelKnobs",
    "StalenessAdvisory",
    "knob_matrix_staleness",
    "load_knob_matrix",
    "resolve_knob_selection",
]
