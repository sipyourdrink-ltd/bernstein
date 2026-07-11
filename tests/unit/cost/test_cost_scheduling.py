"""Cost-aware scheduling: USD budgets, pools, batch and cache policies (#2354).

The scheduler had per-role model policy and per-task ``max_turns`` flags but no
cost model driving dispatch. These tests pin the deterministic cost policy
layer that closes the gap:

* **AC1** - a run halts before exceeding its USD cap, and the halt produces a
  journal-anchored receipt naming the policy inputs and the projected overrun.
* **AC2** - identical ledgers and price tables reproduce byte-identical
  dispatch decisions (deterministic ``decision_hash``); a single changed input
  flips it.
* **AC3** - batch-eligible tasks route to batch endpoints only when the adapter
  exposes a batch surface (capability-mapped); non-eligible tasks never do, and
  an eligible task on a non-batch adapter is refused, not faked.
* **AC4** - fan-out of M workers with a shared prefix issues one warm-up call
  plus M cache-hitting calls when the adapter supports cache windows and the
  policy opts in; the conservative default (off) issues no warm-up and no hits.
* **AC5** - pool exhaustion is surfaced before run start, not mid-run.

Every test fails against the pre-#2354 tree (the modules do not exist yet).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bernstein.adapters._contract import (
    BATCH_DISPATCH_CAPABILITY_MATRIX,
    CACHE_WINDOW_CAPABILITY_MATRIX,
    STRATEGY_MATRIX,
    BatchDispatchCapability,
    CacheWindowCapability,
    batch_dispatch_capability,
)
from bernstein.core.cost.scheduling.batch import BatchRouteDecision, route_batch
from bernstein.core.cost.scheduling.cache_window import (
    CacheFanoutPlan,
    execute_cache_fanout,
    plan_cache_fanout,
)
from bernstein.core.cost.scheduling.policy import (
    CostCaps,
    DispatchCandidate,
    decide_dispatch,
    project_spend,
)
from bernstein.core.cost.scheduling.pools import preflight_pools
from bernstein.core.cost.scheduling.price_table import (
    DEFAULT_PRICE_TABLE,
    DEFAULT_PRICE_TABLE_AS_OF,
    ModelPrice,
    PriceTable,
    load_price_table,
    price_table_staleness,
)
from bernstein.core.cost.scheduling.receipt import (
    build_dispatch_receipt,
    read_dispatch_receipt,
    verify_dispatch_receipt,
)
from bernstein.core.cost.spend_ledger import LedgerEntry
from bernstein.core.security.audit_chain import (
    EVENT_COST_DISPATCH_RECEIPT,
    AuditChainStore,
)

_KEY = b"0" * 32


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _entry(
    *,
    task_id: str = "t1",
    run_id: str = "r1",
    model: str = "sonnet",
    cost_usd: float = 1.0,
    ts: float = 1_762_000_000.0,
    envelope: str = "api",
) -> LedgerEntry:
    return LedgerEntry(
        ts=ts,
        ts_iso=datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds"),
        run_id=run_id,
        task_id=task_id,
        agent_id="a1",
        role="dev",
        feature_label="",
        model=model,
        input_tokens=1000,
        output_tokens=1000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=cost_usd,
        quota_envelope=envelope,
    )


# ---------------------------------------------------------------------------
# Price table (config-backed, schema-validated defaults, staleness advisory)
# ---------------------------------------------------------------------------


def test_default_price_table_prices_known_model() -> None:
    priced = DEFAULT_PRICE_TABLE.price_call("sonnet", input_tokens=1_000_000, output_tokens=1_000_000)
    # generic sonnet row is $3 in / $15 out per 1M
    assert priced.priced is True
    assert priced.cost_usd == pytest.approx(18.0)


def test_price_table_longest_key_wins_over_parent_stem() -> None:
    # "claude-sonnet-5" ($2/$10) must win over the generic "sonnet" row ($3/$15).
    priced = DEFAULT_PRICE_TABLE.price_call("claude-sonnet-5", input_tokens=1_000_000, output_tokens=0)
    assert priced.cost_usd == pytest.approx(2.0)


def test_price_table_unknown_model_is_explicit_zero_not_silent() -> None:
    priced = DEFAULT_PRICE_TABLE.price_call("no-such-model", input_tokens=5000, output_tokens=5000)
    assert priced.priced is False
    assert priced.cost_usd == 0.0


def test_price_table_content_hash_is_stable_and_order_independent() -> None:
    a = PriceTable(
        models={"opus": ModelPrice(5.0, 25.0), "sonnet": ModelPrice(3.0, 15.0)},
        as_of="2026-05-05",
        revision=1,
    )
    b = PriceTable(
        models={"sonnet": ModelPrice(3.0, 15.0), "opus": ModelPrice(5.0, 25.0)},
        as_of="2026-05-05",
        revision=1,
    )
    assert a.content_hash() == b.content_hash()
    assert a.content_hash().startswith("sha256:")
    # A rate change flips the hash.
    c = PriceTable(models={"sonnet": ModelPrice(3.5, 15.0)}, as_of="2026-05-05", revision=1)
    assert c.content_hash() != a.content_hash()


def test_load_price_table_from_config_overrides_defaults_and_validates() -> None:
    table = load_price_table(
        {"my-model": {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 1.0}},
        as_of="2026-07-01",
        revision=7,
    )
    assert table.as_of == "2026-07-01"
    assert table.revision == 7
    priced = table.price_call("my-model", input_tokens=1_000_000, output_tokens=1_000_000)
    assert priced.cost_usd == pytest.approx(3.0)


def test_load_price_table_rejects_negative_rate() -> None:
    with pytest.raises(ValueError, match="negative"):
        load_price_table({"bad": {"input": -1.0, "output": 2.0}})


def test_price_table_staleness_advisory_fires_past_window() -> None:
    fresh = price_table_staleness(DEFAULT_PRICE_TABLE, now_iso="2026-05-20", max_age_days=90)
    assert fresh.stale is False
    stale = price_table_staleness(DEFAULT_PRICE_TABLE, now_iso="2027-01-01", max_age_days=90)
    assert stale.stale is True
    assert stale.age_days > 90
    assert DEFAULT_PRICE_TABLE_AS_OF in stale.message


# ---------------------------------------------------------------------------
# Ledger projection + deterministic dispatch decision (AC1, AC2)
# ---------------------------------------------------------------------------


def test_project_spend_attributes_task_run_day_pool() -> None:
    entries = [
        _entry(task_id="t1", run_id="r1", cost_usd=2.0, ts=1_762_000_000.0, envelope="api"),
        _entry(task_id="t1", run_id="r1", cost_usd=3.0, ts=1_762_000_500.0, envelope="api"),
        _entry(task_id="t2", run_id="r1", cost_usd=4.0, ts=1_762_000_600.0, envelope="subscription"),
    ]
    day = _day_key(1_762_000_000.0)
    spend = project_spend(entries, task_id="t1", run_id="r1", day_key=day, pool="api")
    assert spend.task_usd == pytest.approx(5.0)
    assert spend.run_usd == pytest.approx(9.0)
    assert spend.pool_usd == pytest.approx(5.0)


def test_decide_dispatch_halts_when_run_cap_would_be_exceeded() -> None:
    day = _day_key(1_762_000_000.0)
    entries = [_entry(task_id="t1", run_id="r1", cost_usd=9.5, ts=1_762_000_000.0)]
    caps = CostCaps(per_run_usd=10.0)
    candidate = DispatchCandidate(
        task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=1.0, day_key=day, pool="api"
    )
    decision = decide_dispatch(candidate=candidate, entries=entries, caps=caps, price_table_hash="sha256:deadbeef")
    assert decision.admit is False
    assert decision.breached_dimension == "run"
    assert decision.projected_overrun_usd == pytest.approx(0.5)


def test_decide_dispatch_admits_under_all_caps() -> None:
    day = _day_key(1_762_000_000.0)
    entries = [_entry(task_id="t1", run_id="r1", cost_usd=1.0, ts=1_762_000_000.0)]
    caps = CostCaps(per_task_usd=100.0, per_run_usd=100.0, per_day_usd=100.0)
    candidate = DispatchCandidate(
        task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=2.0, day_key=day, pool="api"
    )
    decision = decide_dispatch(candidate=candidate, entries=entries, caps=caps, price_table_hash="sha256:deadbeef")
    assert decision.admit is True
    assert decision.breached_dimension == ""
    assert decision.projected_overrun_usd == 0.0


def test_decide_dispatch_zero_cap_means_unlimited() -> None:
    day = _day_key(1_762_000_000.0)
    entries = [_entry(cost_usd=1_000.0)]
    caps = CostCaps()  # all zero -> unlimited
    candidate = DispatchCandidate(
        task_id="t1", run_id="r1", model="opus", projected_cost_usd=999.0, day_key=day, pool="api"
    )
    decision = decide_dispatch(candidate=candidate, entries=entries, caps=caps, price_table_hash="sha256:x")
    assert decision.admit is True


def test_decide_dispatch_is_deterministic_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: identical ledgers + price tables reproduce identical decisions."""
    day = _day_key(1_762_000_000.0)
    entries = [_entry(task_id="t1", run_id="r1", cost_usd=4.0, ts=1_762_000_000.0)]
    caps = CostCaps(per_run_usd=5.0)
    candidate = DispatchCandidate(
        task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=2.0, day_key=day, pool="api"
    )
    first = decide_dispatch(candidate=candidate, entries=entries, caps=caps, price_table_hash="sha256:pt")
    second = decide_dispatch(candidate=candidate, entries=list(entries), caps=caps, price_table_hash="sha256:pt")
    assert first.decision_hash == second.decision_hash
    assert first.to_dict() == second.to_dict()


def test_decision_hash_sensitive_to_ledger_and_price_table() -> None:
    day = _day_key(1_762_000_000.0)
    caps = CostCaps(per_run_usd=100.0)
    candidate = DispatchCandidate(
        task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=2.0, day_key=day, pool="api"
    )
    base = decide_dispatch(
        candidate=candidate,
        entries=[_entry(task_id="t1", run_id="r1", cost_usd=1.0)],
        caps=caps,
        price_table_hash="sha256:pt",
    )
    more_ledger = decide_dispatch(
        candidate=candidate,
        entries=[
            _entry(task_id="t1", run_id="r1", cost_usd=1.0),
            _entry(task_id="t1", run_id="r1", cost_usd=1.0),
        ],
        caps=caps,
        price_table_hash="sha256:pt",
    )
    other_price = decide_dispatch(
        candidate=candidate,
        entries=[_entry(task_id="t1", run_id="r1", cost_usd=1.0)],
        caps=caps,
        price_table_hash="sha256:OTHER",
    )
    assert base.decision_hash != more_ledger.decision_hash
    assert base.decision_hash != other_price.decision_hash


# ---------------------------------------------------------------------------
# Halt receipt: journal-anchored, names inputs + overrun, verifies (AC1)
# ---------------------------------------------------------------------------


def test_halt_produces_verifiable_receipt_naming_inputs_and_overrun(tmp_path: Path) -> None:
    day = _day_key(1_762_000_000.0)
    entries = [_entry(task_id="t1", run_id="r1", cost_usd=9.9, ts=1_762_000_000.0)]
    caps = CostCaps(per_run_usd=10.0)
    candidate = DispatchCandidate(
        task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=1.0, day_key=day, pool="api"
    )
    decision = decide_dispatch(candidate=candidate, entries=entries, caps=caps, price_table_hash="sha256:pt-abc")
    assert decision.admit is False

    workdir = tmp_path / "proj"
    lineage_root = workdir / ".sdd" / "lineage"
    chain = AuditChainStore(workdir / ".sdd" / "audit", key=_KEY)
    receipt = build_dispatch_receipt(
        decision=decision,
        workdir=workdir,
        lineage_root=lineage_root,
        hmac_key=_KEY,
        timestamp=1_762_000_001,
        chain=chain,
    )
    payload = receipt.to_dict()
    # Names the policy inputs...
    assert payload["price_table_hash"] == "sha256:pt-abc"
    assert payload["policy_hash"] == decision.policy_hash
    assert payload["ledger_state_hash"] == decision.ledger_state_hash
    # ...and the projected overrun.
    assert payload["projected_overrun_usd"] == pytest.approx(0.9)
    assert payload["breached_dimension"] == "run"
    assert receipt.journal_entry_hash

    # Anchored in the audit chain.
    events = chain.query(event_type=EVENT_COST_DISPATCH_RECEIPT)
    assert len(events) == 1
    assert events[0].details["decision_hash"] == decision.decision_hash
    assert events[0].details["admit"] is False

    # Offline-verifiable against the lineage spine.
    result = verify_dispatch_receipt(
        workdir=workdir, lineage_root=lineage_root, hmac_key=_KEY, decision_hash=decision.decision_hash
    )
    assert result.ok is True


def test_dispatch_receipt_tamper_is_detected(tmp_path: Path) -> None:
    day = _day_key(1_762_000_000.0)
    entries = [_entry(task_id="t1", run_id="r1", cost_usd=9.9, ts=1_762_000_000.0)]
    caps = CostCaps(per_run_usd=10.0)
    candidate = DispatchCandidate(
        task_id="t1", run_id="r1", model="sonnet", projected_cost_usd=1.0, day_key=day, pool="api"
    )
    decision = decide_dispatch(candidate=candidate, entries=entries, caps=caps, price_table_hash="sha256:pt")
    workdir = tmp_path / "proj"
    lineage_root = workdir / ".sdd" / "lineage"
    build_dispatch_receipt(
        decision=decision,
        workdir=workdir,
        lineage_root=lineage_root,
        hmac_key=_KEY,
        timestamp=1_762_000_001,
    )
    stored = read_dispatch_receipt(workdir, decision.decision_hash)
    assert stored is not None
    # Tamper with the on-disk receipt: forge an admit.
    path = workdir / ".sdd" / "cost" / "dispatch" / f"{decision.decision_hash}.json"
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["admit"] = True
    forged["projected_overrun_usd"] = 0.0
    path.write_text(json.dumps(forged), encoding="utf-8")
    result = verify_dispatch_receipt(
        workdir=workdir, lineage_root=lineage_root, hmac_key=_KEY, decision_hash=decision.decision_hash
    )
    assert result.ok is False


# ---------------------------------------------------------------------------
# Batch capability routing (AC3)
# ---------------------------------------------------------------------------


def test_batch_capability_map_covers_every_declared_adapter() -> None:
    # Single source of truth: every strategy-declared adapter has a row.
    for name in STRATEGY_MATRIX:
        assert name in BATCH_DISPATCH_CAPABILITY_MATRIX
        assert isinstance(BATCH_DISPATCH_CAPABILITY_MATRIX[name], BatchDispatchCapability)


def test_batch_eligible_on_capable_adapter_routes_to_batch() -> None:
    # A batch-capable adapter must exist for the routing contract to be real.
    capable = [n for n, c in BATCH_DISPATCH_CAPABILITY_MATRIX.items() if c is BatchDispatchCapability.NATIVE]
    assert capable, "expected at least one batch-capable adapter"
    decision = route_batch(task_id="t1", adapter=capable[0], batch_eligible=True)
    assert isinstance(decision, BatchRouteDecision)
    assert decision.route == "batch"
    assert decision.refused_reason == ""


def test_batch_eligible_on_incapable_adapter_is_refused_not_faked() -> None:
    incapable = [n for n, c in BATCH_DISPATCH_CAPABILITY_MATRIX.items() if c is BatchDispatchCapability.NONE]
    assert incapable
    decision = route_batch(task_id="t1", adapter=incapable[0], batch_eligible=True)
    assert decision.route == "interactive"
    assert decision.refused_reason == "adapter_no_batch_surface"


def test_non_eligible_task_never_routes_to_batch() -> None:
    capable = [n for n, c in BATCH_DISPATCH_CAPABILITY_MATRIX.items() if c is BatchDispatchCapability.NATIVE]
    decision = route_batch(task_id="t1", adapter=capable[0], batch_eligible=False)
    assert decision.route == "interactive"
    # Not a refusal - the task simply was not marked batch-eligible.
    assert decision.refused_reason == ""


def test_unknown_adapter_is_conservatively_non_batch() -> None:
    assert batch_dispatch_capability("totally-unknown") is BatchDispatchCapability.NONE


# ---------------------------------------------------------------------------
# Cache-window fan-out: 1 warm-up + M cache-hitting calls, default off (AC4)
# ---------------------------------------------------------------------------


class _MockCacheAdapter:
    """Records warm-up and worker calls; a worker "hits" iff the prefix is warm."""

    def __init__(self) -> None:
        self._warm: set[str] = set()
        self.warmup_calls = 0
        self.worker_calls = 0

    def warmup(self, prefix: str) -> None:
        self.warmup_calls += 1
        self._warm.add(prefix)

    def worker(self, prefix: str) -> bool:
        self.worker_calls += 1
        return prefix in self._warm


def test_cache_window_capability_map_covers_every_declared_adapter() -> None:
    for name in STRATEGY_MATRIX:
        assert name in CACHE_WINDOW_CAPABILITY_MATRIX


def test_cache_fanout_capable_and_enabled_issues_one_warmup_plus_m_hits() -> None:
    capable = [n for n, c in CACHE_WINDOW_CAPABILITY_MATRIX.items() if c is CacheWindowCapability.SUPPORTED]
    assert capable, "expected at least one cache-window-capable adapter"
    adapter = capable[0]
    plan = plan_cache_fanout(adapter=adapter, worker_count=5, prefix="shared-system-prompt", enabled=True)
    assert isinstance(plan, CacheFanoutPlan)
    assert plan.warmup_calls == 1
    assert plan.fanout_calls == 5
    assert plan.cache_hits_expected == 5

    mock = _MockCacheAdapter()
    result = execute_cache_fanout(
        plan,
        warmup_call=lambda: mock.warmup("shared-system-prompt"),
        worker_call=lambda _i: mock.worker("shared-system-prompt"),
    )
    assert mock.warmup_calls == 1
    assert mock.worker_calls == 5
    assert result.cache_hits == 5


def test_cache_fanout_default_off_races_without_warmup() -> None:
    capable = [n for n, c in CACHE_WINDOW_CAPABILITY_MATRIX.items() if c is CacheWindowCapability.SUPPORTED]
    adapter = capable[0]
    # Conservative default: enabled=False -> no warm-up, no expected hits.
    plan = plan_cache_fanout(adapter=adapter, worker_count=5, prefix="p", enabled=False)
    assert plan.warmup_calls == 0
    assert plan.cache_hits_expected == 0

    mock = _MockCacheAdapter()
    result = execute_cache_fanout(
        plan,
        warmup_call=lambda: mock.warmup("p"),
        worker_call=lambda _i: mock.worker("p"),
    )
    assert mock.warmup_calls == 0
    assert result.cache_hits == 0


def test_cache_fanout_refused_on_incapable_adapter_even_if_enabled() -> None:
    incapable = [n for n, c in CACHE_WINDOW_CAPABILITY_MATRIX.items() if c is CacheWindowCapability.NONE]
    assert incapable
    plan = plan_cache_fanout(adapter=incapable[0], worker_count=4, prefix="p", enabled=True)
    assert plan.warmup_calls == 0
    assert plan.cache_hits_expected == 0
    assert plan.reason == "adapter_no_cache_window"


# ---------------------------------------------------------------------------
# Pool preflight: exhaustion surfaced before run start (AC5)
# ---------------------------------------------------------------------------


def test_pool_preflight_flags_exhaustion_before_run_start() -> None:
    entries = [
        _entry(envelope="api", cost_usd=9.5),
        _entry(envelope="subscription", cost_usd=1.0),
    ]
    report = preflight_pools(
        entries=entries,
        caps={"api": 10.0, "subscription": 100.0},
        planned_usd_by_pool={"api": 1.0},
    )
    assert report.ok is False
    exhausted = {p.pool for p in report.exhausted}
    assert exhausted == {"api"}
    api = next(p for p in report.exhausted if p.pool == "api")
    assert api.projected_usd == pytest.approx(10.5)
    assert api.exhausted is True


def test_pool_preflight_already_over_cap_is_flagged() -> None:
    entries = [_entry(envelope="api", cost_usd=12.0)]
    report = preflight_pools(entries=entries, caps={"api": 10.0})
    assert report.ok is False
    api = report.exhausted[0]
    assert api.already_exhausted is True


def test_pool_preflight_all_within_caps_is_ok() -> None:
    entries = [_entry(envelope="api", cost_usd=1.0)]
    report = preflight_pools(entries=entries, caps={"api": 100.0}, planned_usd_by_pool={"api": 5.0})
    assert report.ok is True
    assert report.exhausted == []


def test_pool_preflight_zero_cap_is_unlimited() -> None:
    entries = [_entry(envelope="api", cost_usd=1_000.0)]
    report = preflight_pools(entries=entries, caps={"api": 0.0})
    assert report.ok is True


# ---------------------------------------------------------------------------
# Config surface: schema-validated price table + caps + pools
# ---------------------------------------------------------------------------


def test_cost_policy_config_schema_validates_and_loads() -> None:
    from bernstein.core.config.config_schema import BernsteinConfig

    cfg = BernsteinConfig.model_validate(
        {
            "goal": "x",
            "cost_policy": {
                "caps": {"per_task_usd": 5.0, "per_run_usd": 20.0, "per_day_usd": 100.0},
                "pools": {"api": 50.0, "subscription": 0.0},
                "cache_window": True,
                "pricing": {
                    "as_of": "2026-07-01",
                    "revision": 3,
                    "models": {"sonnet": {"input": 3.0, "output": 15.0}},
                },
            },
        }
    )
    assert cfg.cost_policy is not None
    assert cfg.cost_policy.caps is not None
    assert cfg.cost_policy.caps.per_run_usd == 20.0
    assert cfg.cost_policy.pools["api"] == 50.0
    assert cfg.cost_policy.cache_window is True
    assert cfg.cost_policy.pricing is not None
    assert cfg.cost_policy.pricing.models["sonnet"].input == 3.0


def test_cost_policy_config_rejects_negative_cap() -> None:
    from pydantic import ValidationError

    from bernstein.core.config.config_schema import BernsteinConfig

    with pytest.raises(ValidationError):
        BernsteinConfig.model_validate({"goal": "x", "cost_policy": {"caps": {"per_run_usd": -1.0}}})


def test_record_cost_dispatch_receipt_is_chain_anchored(tmp_path: Path) -> None:
    from bernstein.core.security.audit_chain import record_cost_dispatch_receipt

    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    event = record_cost_dispatch_receipt(
        chain=chain,
        decision_hash="dh123",
        run_id="r1",
        task_id="t1",
        admit=False,
        breached_dimension="run",
        projected_overrun_usd=0.5,
        price_table_hash="sha256:pt",
        ledger_state_hash="sha256:ls",
        policy_hash="sha256:po",
        journal_entry_hash="sha256:je",
    )
    assert event.event_type == EVENT_COST_DISPATCH_RECEIPT
    assert event.details["prev_chain_digest"] is not None
    ok, errors = chain.verify()
    assert ok, errors
