"""Pure evaluators, budget projection, and remediation selection for SLA
contracts (#2549).

Every function here is a **pure** function of its arguments: it never reads a
file, a clock, or a socket. The supervisor gathers chain evidence from disk and
hands it in as plain data; the resulting verdict is therefore re-derivable by
anyone holding the same evidence segment. This is what lets the violation
receipt verify offline: the receipt embeds the evidence, and
:func:`bernstein.core.orchestration.sla_receipt.verify_receipt` re-runs these
same functions over the embedded bytes.

Determinism discipline: ``tick_instant`` is always passed in (never read from a
wall clock), and every numeric verdict field is rounded to a fixed precision so
two operators produce byte-identical output.

Axis verdicts
-------------
Each axis is a pure function of ``(threshold, evidence rows, tick_instant)``:

* duration -- work-ledger ``task.started`` / ``task.completed`` spans.
* start lateness -- ``schedule.fire`` fire instant vs the observed start.
* fire frequency -- gaps between consecutive ``schedule.fire`` instants.
* freshness -- lineage-spine entries for the maintained artifact; the verdict
  is computed purely from the spine rows (their timestamps and hashes) with no
  filesystem access to the artifact itself.
* spend rate -- spend-ledger rows / the envelope rollup over a window.

Error budget projection
-----------------------
:func:`project_error_budget` is a deterministic projection over a work-ledger
chain segment: remaining budget, burn rate, and escalation tier are recomputable
by anyone holding the same segment, so operator and stakeholder derive identical
numbers from identical history. It generalises the hardcoded
:class:`bernstein.core.observability.slo.ErrorBudget`.

Remediation
-----------
:func:`select_remediation` is a pure function of ``(contract, verdicts)`` -- the
same guarantee :func:`bernstein.core.orchestration.supervisor_receipt.recommend_action`
makes. Contracts are bidirectional: a deadline breach remediates by spending
more (model upgrade), a spend-rate breach by throttling.
:func:`gate_remediation` admits or refuses a spend-more remediation through the
same deterministic budget-envelope dispatch gate the orchestrator uses, and
records the blocked action plus its deterministic fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.observability.slo import ErrorBudgetAction
from bernstein.core.planning.sla_store import (
    AXIS_DURATION,
    AXIS_FREQUENCY,
    AXIS_FRESHNESS,
    AXIS_LATENESS,
    AXIS_SPEND_RATE,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.planning.sla_store import SLAContract

#: Precision used for every rounded numeric verdict field. Fixed so two
#: operators serialise byte-identical receipts.
_ROUND = 6

#: Work-ledger transition kinds that count as a terminal task outcome.
_TERMINAL_KINDS = frozenset({"task.completed", "task.failed", "task.abandoned"})
_FAILED_KINDS = frozenset({"task.failed", "task.abandoned"})


# ---------------------------------------------------------------------------
# Axis verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AxisVerdict:
    """The verdict for one SLA axis, computed purely from evidence.

    Attributes:
        axis: The axis name (e.g. ``"artifact_freshness"``).
        breached: True when the observed value violates the threshold.
        observed: The observed value in the axis' native unit (seconds for the
            time axes, USD/hour for spend rate).
        threshold: The declared contract threshold.
        evidence_hashes: The chain / spine entry hashes the verdict judged,
            sorted for stability. For freshness these are the lineage-spine
            entry hashes, so the receipt embeds exactly what it judged.
        detail: A short human-readable explanation.
    """

    axis: str
    breached: bool
    observed: float
    threshold: float
    evidence_hashes: tuple[str, ...]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "breached": self.breached,
            "observed": round(self.observed, _ROUND),
            "threshold": round(self.threshold, _ROUND),
            "evidence_hashes": list(self.evidence_hashes),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AxisVerdict:
        hashes_raw: Any = raw.get("evidence_hashes", [])
        hashes = tuple(str(h) for h in cast("list[Any]", hashes_raw)) if isinstance(hashes_raw, list) else ()
        return cls(
            axis=str(raw.get("axis", "")),
            breached=bool(raw.get("breached", False)),
            observed=float(raw.get("observed", 0.0)),
            threshold=float(raw.get("threshold", 0.0)),
            evidence_hashes=hashes,
            detail=str(raw.get("detail", "")),
        )


def _rows(evidence: dict[str, Any], axis: str) -> list[dict[str, Any]]:
    raw = evidence.get(axis, [])
    if not isinstance(raw, list):
        return []
    return [cast("dict[str, Any]", r) for r in cast("list[Any]", raw) if isinstance(r, dict)]


def _hashes(rows: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted(str(r.get("entry_hash", "")) for r in rows if r.get("entry_hash")))


# ---------------------------------------------------------------------------
# Per-axis evaluators (pure)
# ---------------------------------------------------------------------------


def evaluate_duration(threshold_s: int, rows: Sequence[dict[str, Any]]) -> AxisVerdict:
    """Verdict for ``max_run_duration``: worst run span vs the threshold.

    Each row is ``{"started", "ended", "entry_hash", ...}`` from the work
    ledger. A run whose ``ended - started`` exceeds ``threshold_s`` breaches.
    """
    worst = 0.0
    for row in rows:
        started = float(row.get("started", 0))
        ended = float(row.get("ended", 0))
        span = max(0.0, ended - started)
        worst = max(worst, span)
    breached = bool(rows) and worst > threshold_s
    return AxisVerdict(
        axis=AXIS_DURATION,
        breached=breached,
        observed=worst,
        threshold=float(threshold_s),
        evidence_hashes=_hashes(rows),
        detail=f"worst run span {worst:.0f}s vs limit {threshold_s}s",
    )


def evaluate_lateness(threshold_s: int, rows: Sequence[dict[str, Any]]) -> AxisVerdict:
    """Verdict for ``start_lateness``: worst (start - fire) vs the threshold.

    Each row is ``{"fire_time", "start_time", "entry_hash", ...}``.
    """
    worst = 0.0
    for row in rows:
        fire = float(row.get("fire_time", 0))
        start = float(row.get("start_time", 0))
        worst = max(worst, max(0.0, start - fire))
    breached = bool(rows) and worst > threshold_s
    return AxisVerdict(
        axis=AXIS_LATENESS,
        breached=breached,
        observed=worst,
        threshold=float(threshold_s),
        evidence_hashes=_hashes(rows),
        detail=f"worst start lateness {worst:.0f}s vs limit {threshold_s}s",
    )


def evaluate_frequency(threshold_s: int, rows: Sequence[dict[str, Any]], now: int) -> AxisVerdict:
    """Verdict for ``fire_frequency``: worst inter-fire gap vs the threshold.

    Each row is ``{"fire_time", "entry_hash", ...}``. The observed gap is the
    max over consecutive fire instants and the trailing gap ``now - last_fire``,
    so a goal that quietly stopped firing breaches even without a new fire.
    """
    fires = sorted(int(r.get("fire_time", 0)) for r in rows)
    if not fires:
        return AxisVerdict(
            axis=AXIS_FREQUENCY,
            breached=False,
            observed=0.0,
            threshold=float(threshold_s),
            evidence_hashes=(),
            detail="no fires recorded; frequency not evaluable",
        )
    worst_gap = float(max(0, now - fires[-1]))
    for prev, curr in pairwise(fires):
        worst_gap = max(worst_gap, float(curr - prev))
    breached = worst_gap > threshold_s
    return AxisVerdict(
        axis=AXIS_FREQUENCY,
        breached=breached,
        observed=worst_gap,
        threshold=float(threshold_s),
        evidence_hashes=_hashes(rows),
        detail=f"worst fire gap {worst_gap:.0f}s vs limit {threshold_s}s",
    )


def evaluate_freshness(
    threshold_s: int,
    artifact_path: str,
    rows: Sequence[dict[str, Any]],
    now: int,
) -> AxisVerdict:
    """Verdict for ``artifact_freshness``, computed purely from spine rows.

    Each row is a lineage-spine entry ``{"artifact_path", "content_hash",
    "timestamp", "entry_hash", ...}``. The freshness is ``now - latest matching
    timestamp``; when the artifact was never re-derived it is treated as maximal
    staleness (a breach). No filesystem access to the artifact occurs.
    """
    matching = [r for r in rows if str(r.get("artifact_path", "")) == artifact_path]
    if not matching:
        return AxisVerdict(
            axis=AXIS_FRESHNESS,
            breached=True,
            observed=float(now),
            threshold=float(threshold_s),
            evidence_hashes=(),
            detail=f"artifact {artifact_path!r} never re-derived",
        )
    latest = max(int(r.get("timestamp", 0)) for r in matching)
    age = float(max(0, now - latest))
    breached = age > threshold_s
    return AxisVerdict(
        axis=AXIS_FRESHNESS,
        breached=breached,
        observed=age,
        threshold=float(threshold_s),
        evidence_hashes=_hashes(matching),
        detail=f"artifact age {age:.0f}s vs limit {threshold_s}s",
    )


def evaluate_spend_rate(
    threshold_usd_per_hour: float,
    rows: Sequence[dict[str, Any]],
    now: int,
) -> AxisVerdict:
    """Verdict for ``spend_rate``: observed USD/hour vs the threshold.

    Each row is a spend-ledger / envelope-rollup entry ``{"cost_usd",
    "timestamp", "entry_hash", ...}``. The window runs from the earliest row to
    ``now``; a zero-width window yields a rate of ``0`` (never a breach) so a
    single point cannot manufacture an infinite rate.
    """
    total = 0.0
    earliest: int | None = None
    for row in rows:
        total += float(row.get("cost_usd", 0.0))
        ts = int(row.get("timestamp", 0))
        if ts > 0 and (earliest is None or ts < earliest):
            earliest = ts
    if earliest is None or now <= earliest:
        rate = 0.0
    else:
        window_hours = (now - earliest) / 3600.0
        rate = total / window_hours if window_hours > 0 else 0.0
    breached = rate > threshold_usd_per_hour
    return AxisVerdict(
        axis=AXIS_SPEND_RATE,
        breached=breached,
        observed=rate,
        threshold=float(threshold_usd_per_hour),
        evidence_hashes=_hashes(rows),
        detail=f"spend rate ${rate:.4f}/h vs limit ${threshold_usd_per_hour:.4f}/h",
    )


def evaluate_all(contract: SLAContract, evidence: dict[str, Any], now: int) -> list[AxisVerdict]:
    """Run every declared axis of ``contract`` over ``evidence`` at ``now``.

    Returns one :class:`AxisVerdict` per declared axis, in canonical axis order.
    ``evidence`` maps an axis name to its list of evidence rows.
    """
    verdicts: list[AxisVerdict] = []
    if contract.max_run_duration_s > 0:
        verdicts.append(evaluate_duration(contract.max_run_duration_s, _rows(evidence, AXIS_DURATION)))
    if contract.start_lateness_s > 0:
        verdicts.append(evaluate_lateness(contract.start_lateness_s, _rows(evidence, AXIS_LATENESS)))
    if contract.fire_frequency_s > 0:
        verdicts.append(evaluate_frequency(contract.fire_frequency_s, _rows(evidence, AXIS_FREQUENCY), now))
    if contract.artifact_freshness_s > 0:
        verdicts.append(
            evaluate_freshness(
                contract.artifact_freshness_s,
                contract.artifact_path,
                _rows(evidence, AXIS_FRESHNESS),
                now,
            )
        )
    if contract.spend_rate_usd_per_hour > 0:
        verdicts.append(evaluate_spend_rate(contract.spend_rate_usd_per_hour, _rows(evidence, AXIS_SPEND_RATE), now))
    return verdicts


def any_breach(verdicts: Sequence[AxisVerdict]) -> bool:
    """True when at least one axis verdict is a breach."""
    return any(v.breached for v in verdicts)


# ---------------------------------------------------------------------------
# Error budget projection (deterministic over a work-ledger segment)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorBudgetProjection:
    """A per-contract error budget projected from a work-ledger segment.

    Every field is a pure function of ``(contract policy, ledger segment)`` --
    no wall clock, no host state -- so two operators holding the same segment
    recompute byte-identical numbers.
    """

    total_events: int
    failed_events: int
    budget_total: int
    budget_remaining: int
    budget_fraction: float
    burn_rate: float
    escalation_tier: str
    is_depleted: bool
    segment_head: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "failed_events": self.failed_events,
            "budget_total": self.budget_total,
            "budget_remaining": self.budget_remaining,
            "budget_fraction": round(self.budget_fraction, _ROUND),
            "burn_rate": round(self.burn_rate, _ROUND),
            "escalation_tier": self.escalation_tier,
            "is_depleted": self.is_depleted,
            "segment_head": self.segment_head,
        }


def _select_tier(contract: SLAContract, burn_rate: float) -> str:
    """Return the highest burn tier whose threshold is <= ``burn_rate``."""
    tier = "ok"
    best = -1.0
    for candidate in sorted(contract.burn_tiers, key=lambda t: t.burn_rate):
        if burn_rate >= candidate.burn_rate and candidate.burn_rate >= best:
            tier = candidate.name
            best = candidate.burn_rate
    return tier


def project_error_budget(contract: SLAContract, ledger_segment: Sequence[dict[str, Any]]) -> ErrorBudgetProjection:
    """Project a per-contract error budget over a work-ledger chain segment.

    Counts terminal task transitions (completed / failed / abandoned) as budget
    events and failed / abandoned transitions as consumed budget. ``burn_rate``
    is the multiple of the budget the failures represent. The escalation tier is
    selected from the contract's burn ladder. ``segment_head`` is the last
    entry hash, so a report names the exact segment it stood on.
    """
    total = 0
    failed = 0
    head = ""
    for entry in ledger_segment:
        kind = str(entry.get("kind", ""))
        entry_hash = str(entry.get("entry_hash", ""))
        if entry_hash:
            head = entry_hash
        if kind in _TERMINAL_KINDS:
            total += 1
            if kind in _FAILED_KINDS:
                failed += 1
    budget_total = max(0, contract.budget_events)
    budget_remaining = max(0, budget_total - failed)
    if budget_total > 0:
        budget_fraction = budget_remaining / budget_total
        burn_rate = failed / budget_total
    else:
        budget_fraction = 1.0 if failed == 0 else 0.0
        burn_rate = 0.0
    is_depleted = budget_remaining <= 0 and total > 0
    tier = _select_tier(contract, burn_rate)
    return ErrorBudgetProjection(
        total_events=total,
        failed_events=failed,
        budget_total=budget_total,
        budget_remaining=budget_remaining,
        budget_fraction=budget_fraction,
        burn_rate=burn_rate,
        escalation_tier=tier,
        is_depleted=is_depleted,
        segment_head=head,
    )


# ---------------------------------------------------------------------------
# Deterministic remediation selection
# ---------------------------------------------------------------------------

_DEADLINE_AXES = frozenset({AXIS_DURATION, AXIS_LATENESS, AXIS_FREQUENCY, AXIS_FRESHNESS})


@dataclass(frozen=True)
class RemediationPlan:
    """The remediation a breach selects, before the cost gate runs.

    Attributes:
        requested_action: The first-choice :class:`ErrorBudgetAction` value.
        spends_more: True when the requested action costs additional spend (a
            model upgrade), so it must clear the budget-envelope gate.
        fallback_action: The deterministic action to fall back to if the gate
            refuses the spend-more remediation.
        reason: A short human-readable explanation.
    """

    requested_action: str
    spends_more: bool
    fallback_action: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_action": self.requested_action,
            "spends_more": self.spends_more,
            "fallback_action": self.fallback_action,
            "reason": self.reason,
        }


def select_remediation(contract: SLAContract, verdicts: Sequence[AxisVerdict]) -> RemediationPlan:
    """Choose a remediation as a pure function of ``(contract, verdicts)``.

    Bidirectional: a deadline breach (duration / lateness / frequency /
    freshness) remediates by spending more (``upgrade_model``); a spend-rate
    breach remediates by throttling (``reduce_agents``); a simultaneous breach
    on both sides resolves to the neutral ``increase_review`` so the two
    pressures never cancel into a spend-more action. The contract is accepted
    for signature symmetry with the other pure selectors and to leave room for
    per-contract policy overrides.
    """
    _ = contract
    breached = {v.axis for v in verdicts if v.breached}
    deadline = bool(breached & _DEADLINE_AXES)
    spend = AXIS_SPEND_RATE in breached
    if deadline and spend:
        return RemediationPlan(
            requested_action=ErrorBudgetAction.INCREASE_REVIEW.value,
            spends_more=False,
            fallback_action=ErrorBudgetAction.INCREASE_REVIEW.value,
            reason="deadline and spend both breached; neutral remediation",
        )
    if deadline:
        return RemediationPlan(
            requested_action=ErrorBudgetAction.UPGRADE_MODEL.value,
            spends_more=True,
            fallback_action=ErrorBudgetAction.INCREASE_REVIEW.value,
            reason="deadline breach; upgrade model to recover turnaround",
        )
    if spend:
        return RemediationPlan(
            requested_action=ErrorBudgetAction.REDUCE_AGENTS.value,
            spends_more=False,
            fallback_action=ErrorBudgetAction.REDUCE_AGENTS.value,
            reason="spend-rate breach; throttle concurrency",
        )
    return RemediationPlan(
        requested_action="",
        spends_more=False,
        fallback_action="",
        reason="no breach",
    )


# ---------------------------------------------------------------------------
# Cost-aware gating (dispatch-gate interaction)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemediationDecision:
    """The gated remediation decision embedded in the receipt.

    Attributes:
        requested_action: The action the plan selected.
        effective_action: The action after gating (the fallback when blocked).
        admitted: True when the spend-more remediation cleared the budget gate
            (or when no gate applied).
        blocked: True when the gate refused the spend-more remediation.
        decision_hash: The dispatch decision hash (empty when no gate applied).
        breached_dimension: The budget dimension that halted the remediation
            (empty when admitted).
        caps: The USD caps the gate enforced (``{}`` when no gate applied).
        remediation_cost_usd: The projected extra spend the gate weighed.
    """

    requested_action: str
    effective_action: str
    admitted: bool
    blocked: bool
    decision_hash: str
    breached_dimension: str
    caps: dict[str, float] = field(default_factory=dict[str, float])
    remediation_cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_action": self.requested_action,
            "effective_action": self.effective_action,
            "admitted": self.admitted,
            "blocked": self.blocked,
            "decision_hash": self.decision_hash,
            "breached_dimension": self.breached_dimension,
            "caps": {k: round(float(v), _ROUND) for k, v in sorted(self.caps.items())},
            "remediation_cost_usd": round(self.remediation_cost_usd, _ROUND),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RemediationDecision:
        caps_raw: Any = raw.get("caps", {})
        caps = (
            {str(k): float(v) for k, v in cast("dict[str, Any]", caps_raw).items()}
            if isinstance(caps_raw, dict)
            else {}
        )
        return cls(
            requested_action=str(raw.get("requested_action", "")),
            effective_action=str(raw.get("effective_action", "")),
            admitted=bool(raw.get("admitted", False)),
            blocked=bool(raw.get("blocked", False)),
            decision_hash=str(raw.get("decision_hash", "")),
            breached_dimension=str(raw.get("breached_dimension", "")),
            caps=caps,
            remediation_cost_usd=float(raw.get("remediation_cost_usd", 0.0)),
        )


def _day_key(tick_instant: int) -> str:
    return datetime.fromtimestamp(tick_instant, tz=UTC).strftime("%Y-%m-%d")


def gate_remediation(
    plan: RemediationPlan,
    *,
    spend_rows: Sequence[dict[str, Any]],
    caps: dict[str, float] | None,
    remediation_cost_usd: float,
    tick_instant: int,
    price_table_hash: str = "sla-remediation",
) -> RemediationDecision:
    """Admit or refuse a spend-more remediation through the budget gate.

    A remediation that does not spend more (throttle / review) is admitted
    unconditionally. A spend-more remediation is priced as a synthetic dispatch
    candidate and run through :func:`decide_dispatch` against the embedded spend
    rows and the operator caps; when the projected extra spend would breach a
    cap the remediation is blocked and the plan's deterministic fallback becomes
    the effective action, both of which are recorded here (and, by the caller,
    on the audit chain).

    Pure: the same plan, spend rows, caps, cost, and tick reproduce the
    byte-identical decision hash, so a verifier re-runs it from the receipt.
    """
    if not plan.spends_more or not caps or all(v <= 0 for v in caps.values()):
        return RemediationDecision(
            requested_action=plan.requested_action,
            effective_action=plan.requested_action,
            admitted=True,
            blocked=False,
            decision_hash="",
            breached_dimension="",
            caps=dict(caps or {}),
            remediation_cost_usd=remediation_cost_usd,
        )

    from bernstein.core.cost.scheduling.policy import CostCaps, DispatchCandidate, decide_dispatch
    from bernstein.core.cost.spend_ledger import LedgerEntry

    cost_caps = CostCaps(
        per_task_usd=float(caps.get("per_task_usd", 0.0)),
        per_run_usd=float(caps.get("per_run_usd", 0.0)),
        per_day_usd=float(caps.get("per_day_usd", 0.0)),
    )
    day = _day_key(tick_instant)
    ts_iso = datetime.fromtimestamp(tick_instant, tz=UTC).isoformat(timespec="seconds")
    run_id = "sla-remediation"
    entries = [
        LedgerEntry(
            ts=float(tick_instant),
            ts_iso=ts_iso,
            run_id=run_id,
            task_id="",
            agent_id="",
            role="",
            feature_label="",
            model="",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=float(row.get("cost_usd", 0.0)),
            quota_envelope="subscription",
        )
        for row in spend_rows
    ]
    candidate = DispatchCandidate(
        task_id="sla-remediation",
        run_id=run_id,
        model="opus",
        projected_cost_usd=max(0.0, float(remediation_cost_usd)),
        day_key=day,
        pool="subscription",
    )
    decision = decide_dispatch(
        candidate=candidate,
        entries=entries,
        caps=cost_caps,
        price_table_hash=price_table_hash,
    )
    if decision.admit:
        return RemediationDecision(
            requested_action=plan.requested_action,
            effective_action=plan.requested_action,
            admitted=True,
            blocked=False,
            decision_hash=decision.decision_hash,
            breached_dimension="",
            caps=dict(caps),
            remediation_cost_usd=remediation_cost_usd,
        )
    return RemediationDecision(
        requested_action=plan.requested_action,
        effective_action=plan.fallback_action,
        admitted=False,
        blocked=True,
        decision_hash=decision.decision_hash,
        breached_dimension=decision.breached_dimension,
        caps=dict(caps),
        remediation_cost_usd=remediation_cost_usd,
    )


__all__ = [
    "AxisVerdict",
    "ErrorBudgetProjection",
    "RemediationDecision",
    "RemediationPlan",
    "any_breach",
    "evaluate_all",
    "evaluate_duration",
    "evaluate_frequency",
    "evaluate_freshness",
    "evaluate_lateness",
    "evaluate_spend_rate",
    "gate_remediation",
    "project_error_budget",
    "select_remediation",
]
