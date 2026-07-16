"""Tick-level dispatch gate: consult ``decide_dispatch`` before a live spawn (#2354).

The shipped policy layer (:func:`~bernstein.core.cost.scheduling.policy.decide_dispatch`
plus the dispatch receipt) is a pure, deterministic decision, but on its own it
decides nothing about a real run -- the orchestrator has to *consult* it before
each dispatch. This module is that thin, deterministic bridge between the
orchestrator's spawn loop and the policy layer:

* :func:`resolve_cost_caps` / :func:`resolve_price_table` turn the operator's
  ``cost_policy`` config block into the policy inputs, with a clean fail-open
  no-op (``None`` caps) when no policy -- or no cap -- is configured. A missing
  or disabled price policy is therefore a no-op, never a spurious halt.
* :func:`build_dispatch_candidates` costs the batches the orchestrator is about
  to spawn this tick from the per-task estimates it already computed.
* :func:`evaluate_run_dispatch` walks those candidates in dispatch order and
  folds each admitted candidate's projected spend back through the *same*
  :func:`decide_dispatch` projection (as a synthetic ledger row), so the first
  candidate that would push a dimension over its cap halts the tick
  (fail-closed) and its :class:`~bernstein.core.cost.scheduling.policy.DispatchDecision`
  is returned for the caller to seal into a receipt.

Everything here is a pure function of its arguments (no clock, no filesystem, no
network): the same ledger + caps + price table + candidate order reproduce the
byte-identical halt decision, so a replay lands on the same ``decision_hash``
the orchestrator sealed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bernstein.core.cost.scheduling.knob_matrix import (
    DEFAULT_KNOB_MATRIX,
    KnobMatrix,
    load_knob_matrix,
    resolve_knob_selection,
)
from bernstein.core.cost.scheduling.policy import (
    CostCaps,
    DispatchCandidate,
    DispatchDecision,
    decide_dispatch,
)
from bernstein.core.cost.scheduling.price_table import (
    DEFAULT_PRICE_TABLE,
    PriceTable,
    load_price_table,
)
from bernstein.core.cost.spend_ledger import LedgerEntry

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


def resolve_cost_caps(cost_policy: Any | None) -> CostCaps | None:
    """Resolve the enforced USD ceilings, or ``None`` for a no-op (fail-open).

    Returns ``None`` -- meaning "do not gate this run" -- when no cost policy is
    configured, when the policy carries no ``caps`` block, or when every cap is
    ``0`` (unlimited). Only a policy that names at least one positive ceiling
    produces a :class:`CostCaps`, so an absent or disabled policy never halts a
    dispatch.
    """
    if cost_policy is None:
        return None
    caps = getattr(cost_policy, "caps", None)
    if caps is None:
        return None
    per_task = float(getattr(caps, "per_task_usd", 0.0) or 0.0)
    per_run = float(getattr(caps, "per_run_usd", 0.0) or 0.0)
    per_day = float(getattr(caps, "per_day_usd", 0.0) or 0.0)
    if per_task <= 0.0 and per_run <= 0.0 and per_day <= 0.0:
        return None
    return CostCaps(per_task_usd=per_task, per_run_usd=per_run, per_day_usd=per_day)


def _pricing_models(pricing: Any) -> dict[str, dict[str, Any]]:
    """Flatten a config ``pricing.models`` block into plain rate rows."""
    raw = getattr(pricing, "models", None)
    if not raw:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for key, price in raw.items():
        if hasattr(price, "model_dump"):
            row = price.model_dump()
        elif isinstance(price, dict):
            row = dict(price)
        else:
            row = {
                "input": getattr(price, "input", 0.0),
                "output": getattr(price, "output", 0.0),
                "cache_read": getattr(price, "cache_read", 0.0),
                "cache_write": getattr(price, "cache_write", 0.0),
            }
        rows[str(key)] = row
    return rows


def resolve_price_table(cost_policy: Any | None) -> PriceTable:
    """Resolve the hash-pinned price table the dispatch cost was priced against.

    Config ``cost_policy.pricing`` overrides the shipped defaults (same contract
    as :func:`~bernstein.core.cost.scheduling.price_table.load_price_table`); an
    absent pricing block yields :data:`DEFAULT_PRICE_TABLE` unchanged. The table
    is never probed over the network -- its :meth:`PriceTable.content_hash` is
    what the receipt names.
    """
    if cost_policy is None:
        return DEFAULT_PRICE_TABLE
    pricing = getattr(cost_policy, "pricing", None)
    if pricing is None:
        return DEFAULT_PRICE_TABLE
    models = _pricing_models(pricing)
    if not models:
        return DEFAULT_PRICE_TABLE
    return load_price_table(
        models,
        as_of=(getattr(pricing, "as_of", "") or None),
        revision=int(getattr(pricing, "revision", 0) or 0),
    )


def _knob_models(knobs: Any) -> dict[str, dict[str, Any]]:
    """Flatten a config ``knobs.models`` block into plain knob rows."""
    raw = getattr(knobs, "models", None)
    if not raw:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if hasattr(entry, "model_dump"):
            row = entry.model_dump()
        elif isinstance(entry, dict):
            row = dict(entry)
        else:
            row = {
                "effort_levels": getattr(entry, "effort_levels", None),
                "default_effort": getattr(entry, "default_effort", None),
                "lanes": getattr(entry, "lanes", None),
                "cache_strategies": getattr(entry, "cache_strategies", None),
            }
        rows[str(key)] = row
    return rows


def resolve_knob_matrix(cost_policy: Any | None) -> KnobMatrix:
    """Resolve the hash-pinned dispatch knob matrix (#2519).

    Config ``cost_policy.knobs`` overrides the shipped defaults (same contract
    as :func:`~bernstein.core.cost.scheduling.knob_matrix.load_knob_matrix`); an
    absent knobs block yields :data:`DEFAULT_KNOB_MATRIX` unchanged. The matrix
    is never probed -- its :meth:`KnobMatrix.content_hash` is what the sealed
    knob selection names in every decision.
    """
    if cost_policy is None:
        return DEFAULT_KNOB_MATRIX
    knobs = getattr(cost_policy, "knobs", None)
    if knobs is None:
        return DEFAULT_KNOB_MATRIX
    models = _knob_models(knobs)
    if not models:
        return DEFAULT_KNOB_MATRIX
    return load_knob_matrix(
        models,
        as_of=(getattr(knobs, "as_of", "") or None),
        revision=int(getattr(knobs, "revision", 0) or 0),
    )


def build_dispatch_candidates(
    batches: Iterable[Any],
    *,
    cost_estimates: dict[str, float],
    run_id: str,
    day_key: str,
    pool: str = "",
    knob_matrix: KnobMatrix | None = None,
) -> list[DispatchCandidate]:
    """Build one :class:`DispatchCandidate` per about-to-spawn batch.

    A batch becomes a single agent, so its projected cost is the sum of its
    tasks' pre-spawn estimates (a task with no estimate contributes ``0``, i.e.
    it never manufactures spend). The candidate is attributed to the batch's
    lead task for the per-task dimension. Empty batches are skipped.

    When *knob_matrix* is supplied, each candidate's per-call knobs (effort,
    lane, cache strategy) are resolved deterministically and sealed onto the
    candidate, and the projected cost is multiplied by the resolved lane
    multiplier -- so admit and halt decisions account for the lane and effort
    actually chosen (#2519). The sealed selection folds into the decision hash
    downstream. With no matrix the behaviour is byte-identical to the pre-#2519
    contract (multiplier ``1.0``, no knob selection).

    Args:
        batches: Role-grouped batches of Task-like objects (each item is
            iterable and indexable, exposing ``id`` and optional ``model`` /
            ``adapter`` / ``effort`` / ``is_batch`` / ``cache_strategy``).
        cost_estimates: ``task_id -> estimated_cost_usd`` from the tick.
        run_id: The active run id (attributed to every candidate).
        day_key: UTC ``YYYY-MM-DD`` bucket for the day dimension.
        pool: Optional quota pool attributed to the candidates.
        knob_matrix: The pinned knob matrix to resolve per-call knobs against,
            or ``None`` to keep the pre-#2519 projection unchanged.

    Returns:
        Candidates in the batches' dispatch order.
    """
    candidates: list[DispatchCandidate] = []
    for batch in batches:
        tasks = list(batch)
        if not tasks:
            continue
        lead = tasks[0]
        projected = sum(float(cost_estimates.get(getattr(task, "id", ""), 0.0)) for task in tasks)
        candidate = DispatchCandidate(
            task_id=str(getattr(lead, "id", "")),
            run_id=run_id,
            model=str(getattr(lead, "model", "") or ""),
            projected_cost_usd=projected,
            day_key=day_key,
            pool=pool,
            adapter=str(getattr(lead, "adapter", "") or ""),
            requested_effort=str(getattr(lead, "effort", "") or ""),
            batch_eligible=bool(getattr(lead, "is_batch", False)),
            requested_cache=str(getattr(lead, "cache_strategy", "") or ""),
        )
        if knob_matrix is not None:
            selection = resolve_knob_selection(candidate=candidate, matrix=knob_matrix)
            candidate = replace(
                candidate,
                projected_cost_usd=projected * selection.rate_multiplier,
                knob_selection=selection,
            )
        candidates.append(candidate)
    return candidates


@dataclass(frozen=True, slots=True)
class RunDispatchOutcome:
    """Result of gating one tick's worth of dispatch candidates.

    Attributes:
        admitted: Decisions for the candidates cleared to dispatch, in order.
        halt: The first candidate's decision that would breach a cap, or
            ``None`` when every candidate is admitted. When set, the caller
            must not dispatch and should seal this decision into a receipt.
    """

    admitted: tuple[DispatchDecision, ...]
    halt: DispatchDecision | None

    @property
    def admit(self) -> bool:
        """True when no candidate would breach a cap (nothing to halt)."""
        return self.halt is None


def _committed_entry(candidate: DispatchCandidate, *, now_ts: float) -> LedgerEntry:
    """Synthesize a ledger row for an admitted candidate's committed spend.

    Threading admitted spend back through :func:`decide_dispatch` as a real
    ledger row keeps a single source of truth for the cap comparison: the next
    candidate's run / day / task projection naturally includes what this tick
    has already committed, so a batch of small dispatches that only collectively
    breach still halts on the exact candidate that tips a dimension over.
    """
    return LedgerEntry(
        ts=now_ts,
        ts_iso=datetime.fromtimestamp(now_ts, tz=UTC).isoformat(timespec="seconds"),
        run_id=candidate.run_id,
        task_id=candidate.task_id,
        agent_id="",
        role="",
        feature_label="",
        model=candidate.model,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=max(0.0, candidate.projected_cost_usd),
        quota_envelope=candidate.pool or "subscription",
    )


def evaluate_run_dispatch(
    *,
    candidates: Sequence[DispatchCandidate],
    entries: Sequence[LedgerEntry],
    caps: CostCaps,
    price_table_hash: str,
    now_ts: float,
) -> RunDispatchOutcome:
    """Gate a tick's dispatch candidates against the USD caps (AC1).

    Walks *candidates* in order; each is decided by :func:`decide_dispatch`
    against the real ledger *entries* plus the spend already committed by
    earlier admitted candidates this tick. The first candidate that halts stops
    the walk and is returned as :attr:`RunDispatchOutcome.halt`; if none halts,
    every candidate is admitted.

    Args:
        candidates: This tick's dispatch candidates, in dispatch order.
        entries: The persisted spend ledger (read-only projection input).
        caps: The USD ceilings to enforce.
        price_table_hash: Content hash of the pinned price table (named in each
            decision so a verifier recomputes it byte-identically).
        now_ts: Timestamp used to bucket synthetic within-tick spend into the
            same day as the candidates.

    Returns:
        A :class:`RunDispatchOutcome`.
    """
    base_entries = list(entries)
    committed: list[LedgerEntry] = []
    admitted: list[DispatchDecision] = []
    for candidate in candidates:
        decision = decide_dispatch(
            candidate=candidate,
            entries=[*base_entries, *committed],
            caps=caps,
            price_table_hash=price_table_hash,
        )
        if not decision.admit:
            return RunDispatchOutcome(admitted=tuple(admitted), halt=decision)
        admitted.append(decision)
        committed.append(_committed_entry(candidate, now_ts=now_ts))
    return RunDispatchOutcome(admitted=tuple(admitted), halt=None)


__all__ = [
    "RunDispatchOutcome",
    "build_dispatch_candidates",
    "evaluate_run_dispatch",
    "resolve_cost_caps",
    "resolve_knob_matrix",
    "resolve_price_table",
]
