"""Tick-level dispatch gate that wires ``decide_dispatch`` into a live run (#2354).

The shipped policy layer (``decide_dispatch`` + the dispatch receipt) was pure
and unit-tested, but nothing consulted it before a real spawn, so a run only
learned it had blown its USD cap after the fact. These tests pin the thin,
deterministic gate that closes that gap:

* ``resolve_cost_caps`` / ``resolve_price_table`` -- config resolution with a
  clean fail-open no-op when no cost policy (or no cap) is configured.
* ``build_dispatch_candidates`` -- one candidate per about-to-spawn batch,
  costed from the tick's per-task estimates.
* ``evaluate_run_dispatch`` -- walks the tick's candidates in dispatch order,
  threading each admitted candidate's projected spend back through the same
  ``decide_dispatch`` projection so the first candidate that would breach a cap
  halts the tick (fail-closed), and returns that halting decision.

Every function is a pure projection of its inputs (no clock, no filesystem, no
network) so a replay with the same ledger + caps + price table reproduces the
byte-identical halt decision.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from bernstein.core.config.config_schema import (
    CostCapsSchema,
    CostPolicySchema,
    ModelPriceSchema,
    PricingSchema,
)
from bernstein.core.cost.scheduling.dispatch_gate import (
    RunDispatchOutcome,
    build_dispatch_candidates,
    evaluate_run_dispatch,
    resolve_cost_caps,
    resolve_price_table,
)
from bernstein.core.cost.scheduling.policy import CostCaps, DispatchCandidate
from bernstein.core.cost.scheduling.price_table import DEFAULT_PRICE_TABLE
from bernstein.core.cost.spend_ledger import LedgerEntry

_NOW_TS = 1_762_000_000.0


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _entry(
    *,
    task_id: str = "t1",
    run_id: str = "r1",
    cost_usd: float = 1.0,
    ts: float = _NOW_TS,
    envelope: str = "api",
) -> LedgerEntry:
    return LedgerEntry(
        ts=ts,
        ts_iso=datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds"),
        run_id=run_id,
        task_id=task_id,
        agent_id="a1",
        role="backend",
        feature_label="",
        model="claude-sonnet-5",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=cost_usd,
        quota_envelope=envelope,
    )


class _Batch:
    """Minimal stand-in for a role-grouped batch of Task-like objects."""

    def __init__(self, *tasks: object) -> None:
        self._tasks = list(tasks)

    def __iter__(self) -> Iterator[object]:
        return iter(self._tasks)

    def __getitem__(self, idx: int) -> object:
        return self._tasks[idx]

    def __len__(self) -> int:
        return len(self._tasks)


class _Task:
    def __init__(self, task_id: str, model: str | None = "claude-sonnet-5") -> None:
        self.id = task_id
        self.model = model


# ---------------------------------------------------------------------------
# Config resolution: fail-open no-op when nothing is configured
# ---------------------------------------------------------------------------


def test_resolve_cost_caps_none_when_policy_absent() -> None:
    assert resolve_cost_caps(None) is None


def test_resolve_cost_caps_none_when_caps_absent() -> None:
    assert resolve_cost_caps(CostPolicySchema()) is None


def test_resolve_cost_caps_none_when_all_caps_zero() -> None:
    policy = CostPolicySchema(caps=CostCapsSchema())  # every dimension unlimited (0)
    assert resolve_cost_caps(policy) is None


def test_resolve_cost_caps_maps_configured_ceilings() -> None:
    policy = CostPolicySchema(caps=CostCapsSchema(per_task_usd=1.0, per_run_usd=10.0, per_day_usd=50.0))
    caps = resolve_cost_caps(policy)
    assert caps == CostCaps(per_task_usd=1.0, per_run_usd=10.0, per_day_usd=50.0)


def test_resolve_price_table_defaults_when_no_pricing() -> None:
    table = resolve_price_table(None)
    assert table.content_hash() == DEFAULT_PRICE_TABLE.content_hash()
    assert resolve_price_table(CostPolicySchema()).content_hash() == DEFAULT_PRICE_TABLE.content_hash()


def test_resolve_price_table_applies_config_override() -> None:
    policy = CostPolicySchema(
        pricing=PricingSchema(
            as_of="2026-07-01",
            revision=9,
            models={"my-model": ModelPriceSchema(input=1.0, output=2.0)},
        )
    )
    table = resolve_price_table(policy)
    assert table.content_hash() != DEFAULT_PRICE_TABLE.content_hash()
    priced = table.price_call("my-model", input_tokens=1_000_000, output_tokens=1_000_000)
    assert priced.cost_usd == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Candidate construction from about-to-spawn batches
# ---------------------------------------------------------------------------


def test_build_dispatch_candidates_sums_batch_estimates() -> None:
    batches = [_Batch(_Task("t1"), _Task("t2")), _Batch(_Task("t3"))]
    estimates = {"t1": 1.5, "t2": 2.0, "t3": 4.0}
    candidates = build_dispatch_candidates(batches, cost_estimates=estimates, run_id="r1", day_key=_day_key(_NOW_TS))
    assert len(candidates) == 2
    assert candidates[0].task_id == "t1"  # lead task of the first batch
    assert candidates[0].projected_cost_usd == pytest.approx(3.5)
    assert candidates[0].run_id == "r1"
    assert candidates[1].projected_cost_usd == pytest.approx(4.0)


def test_build_dispatch_candidates_missing_estimate_contributes_zero() -> None:
    batches = [_Batch(_Task("t1"), _Task("t2"))]
    candidates = build_dispatch_candidates(batches, cost_estimates={"t1": 2.0}, run_id="r1", day_key=_day_key(_NOW_TS))
    assert candidates[0].projected_cost_usd == pytest.approx(2.0)


def test_build_dispatch_candidates_skips_empty_batches() -> None:
    batches = [_Batch(), _Batch(_Task("t9"))]
    candidates = build_dispatch_candidates(batches, cost_estimates={"t9": 1.0}, run_id="r1", day_key=_day_key(_NOW_TS))
    assert [c.task_id for c in candidates] == ["t9"]


# ---------------------------------------------------------------------------
# evaluate_run_dispatch: admit / halt / within-tick accumulation
# ---------------------------------------------------------------------------


def test_evaluate_run_dispatch_admits_all_under_cap() -> None:
    caps = CostCaps(per_run_usd=100.0)
    candidates = [
        DispatchCandidate(task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=2.0, day_key=_day_key(_NOW_TS)),
    ]
    outcome = evaluate_run_dispatch(
        candidates=candidates,
        entries=[_entry(task_id="t0", run_id="r1", cost_usd=1.0)],
        caps=caps,
        price_table_hash="sha256:pt",
        now_ts=_NOW_TS,
    )
    assert isinstance(outcome, RunDispatchOutcome)
    assert outcome.halt is None
    assert outcome.admit is True
    assert len(outcome.admitted) == 1


def test_evaluate_run_dispatch_halts_first_candidate_that_breaches_run_cap() -> None:
    caps = CostCaps(per_run_usd=10.0)
    entries = [_entry(task_id="t0", run_id="r1", cost_usd=9.5)]
    candidate = DispatchCandidate(
        task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=1.0, day_key=_day_key(_NOW_TS)
    )
    outcome = evaluate_run_dispatch(
        candidates=[candidate], entries=entries, caps=caps, price_table_hash="sha256:pt", now_ts=_NOW_TS
    )
    assert outcome.admit is False
    assert outcome.halt is not None
    assert outcome.halt.admit is False
    assert outcome.halt.breached_dimension == "run"
    assert outcome.halt.projected_overrun_usd == pytest.approx(0.5)
    assert outcome.halt.verify_self_hash() is True


def test_evaluate_run_dispatch_accumulates_within_tick_across_candidates() -> None:
    """Two candidates each fit alone but together breach: the second halts."""
    caps = CostCaps(per_run_usd=10.0)
    cand1 = DispatchCandidate(
        task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=6.0, day_key=_day_key(_NOW_TS)
    )
    cand2 = DispatchCandidate(
        task_id="t2", run_id="r1", model="sonnet", projected_cost_usd=6.0, day_key=_day_key(_NOW_TS)
    )
    outcome = evaluate_run_dispatch(
        candidates=[cand1, cand2], entries=[], caps=caps, price_table_hash="sha256:pt", now_ts=_NOW_TS
    )
    assert outcome.halt is not None
    assert outcome.halt.task_id == "t2"
    assert outcome.halt.breached_dimension == "run"
    # cand1's committed spend (6.0) is folded into cand2's run projection.
    assert outcome.halt.prior_run_usd == pytest.approx(6.0)
    assert outcome.halt.projected_overrun_usd == pytest.approx(2.0)
    assert [c.task_id for c in outcome.admitted] == ["t1"]


def test_evaluate_run_dispatch_is_deterministic_replay() -> None:
    caps = CostCaps(per_run_usd=10.0)
    entries = [_entry(task_id="t0", run_id="r1", cost_usd=9.9)]
    candidate = DispatchCandidate(
        task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=1.0, day_key=_day_key(_NOW_TS)
    )
    first = evaluate_run_dispatch(
        candidates=[candidate], entries=entries, caps=caps, price_table_hash="sha256:pt", now_ts=_NOW_TS
    )
    second = evaluate_run_dispatch(
        candidates=[candidate], entries=list(entries), caps=caps, price_table_hash="sha256:pt", now_ts=_NOW_TS
    )
    assert first.halt is not None
    assert second.halt is not None
    assert first.halt.decision_hash == second.halt.decision_hash


def test_evaluate_run_dispatch_empty_candidates_admits() -> None:
    outcome = evaluate_run_dispatch(
        candidates=[],
        entries=[_entry(cost_usd=100.0)],
        caps=CostCaps(per_run_usd=1.0),
        price_table_hash="sha256:pt",
        now_ts=_NOW_TS,
    )
    assert outcome.admit is True
    assert outcome.halt is None
