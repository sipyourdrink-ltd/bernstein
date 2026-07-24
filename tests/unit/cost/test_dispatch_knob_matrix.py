"""Dispatch knob matrix: knobs pinned into the deterministic fingerprint (#2519).

The cost-aware dispatch policy priced a spawn from a USD projection keyed by
model name only; the per-call knobs that move the price of an identical task
(reasoning effort, processing lane, cache strategy) were scattered and unpinned,
so two runs of the same plan could pay differently and produce receipts that
verify equally well. These tests pin the fix:

* **Determinism** -- two processes given the same candidate + matrix produce
  byte-identical ``KnobSelection`` canonical JSON and the identical decision
  hash; a single knob change flips the decision hash.
* **Verifiability** -- a sealed receipt verifies offline; mutating any single
  knob field (effort, lane, cache strategy, multiplier) fails verification with
  the mismatching field named.
* **Determinism (replay)** -- a run whose journal recorded knob selections
  reproduces the same head hash; a forced knob change surfaces as divergence at
  the exact step index.
* **Correctness** -- a model absent from the matrix resolves to an explicit
  default carrying ``resolved=False`` and a machine-readable reason, multiplier
  ``1.0``, so the admit/halt outcome is unchanged for unpriced models.
* **Observability** -- candidate costing applies the resolved lane multiplier;
  a cap halt caused by a lane/effort choice names that knob in the receipt.
* **Purity** -- the resolver reads no clock, filesystem, or network.

Every test fails against the pre-#2519 tree (the knob matrix does not exist yet).
"""

from __future__ import annotations

import builtins
import json
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bernstein.core.cost.scheduling.dispatch_gate import (
    build_dispatch_candidates,
    evaluate_run_dispatch,
    resolve_knob_matrix,
)
from bernstein.core.cost.scheduling.knob_matrix import (
    CACHE_NONE,
    CACHE_WARM_UP,
    DEFAULT_KNOB_MATRIX,
    LANE_BATCH,
    LANE_INTERACTIVE,
    REASON_MODEL_NOT_IN_MATRIX,
    KnobMatrix,
    knob_matrix_staleness,
    load_knob_matrix,
    resolve_knob_selection,
)
from bernstein.core.cost.scheduling.policy import (
    KNOB_FIELDS,
    CostCaps,
    DispatchCandidate,
    KnobSelection,
    decide_dispatch,
)
from bernstein.core.cost.scheduling.receipt import (
    build_dispatch_receipt,
    dispatch_receipt_path,
    verify_dispatch_receipt,
)
from bernstein.core.cost.spend_ledger import LedgerEntry
from bernstein.core.replay.journal import (
    DISPATCH_KNOB_SELECTION_EVENT,
    EventJournal,
    record_dispatch_knob_selection,
)

_KEY = b"0" * 32


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _entry(*, task_id: str = "t1", run_id: str = "r1", model: str = "opus", cost_usd: float = 1.0) -> LedgerEntry:
    ts = 1_762_000_000.0
    return LedgerEntry(
        ts=ts,
        ts_iso=datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds"),
        run_id=run_id,
        task_id=task_id,
        agent_id="a1",
        role="dev",
        feature_label="",
        model=model,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=cost_usd,
        quota_envelope="api",
    )


@dataclass
class _Task:
    """Minimal Task-like object for build_dispatch_candidates."""

    id: str
    model: str = "opus"
    adapter: str = "claude"
    effort: str = ""
    is_batch: bool = False
    cache_strategy: str = ""


def _candidate(**overrides: object) -> DispatchCandidate:
    base: dict[str, object] = {
        "task_id": "t1",
        "run_id": "r1",
        "model": "opus",
        "projected_cost_usd": 1.0,
        "adapter": "claude",
        "requested_effort": "high",
        "batch_eligible": False,
        "requested_cache": "",
    }
    base.update(overrides)
    return DispatchCandidate(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# KnobMatrix: content-addressed, config-overridable, derived defaults
# ---------------------------------------------------------------------------


def test_default_matrix_is_content_addressed_and_stable() -> None:
    assert DEFAULT_KNOB_MATRIX.content_hash().startswith("sha256:")
    # Rebuilding the same matrix content hashes byte-identically.
    rebuilt = KnobMatrix(models=dict(DEFAULT_KNOB_MATRIX.models), as_of=DEFAULT_KNOB_MATRIX.as_of, revision=1)
    assert rebuilt.content_hash() == DEFAULT_KNOB_MATRIX.content_hash()


def test_default_matrix_declares_known_models_and_lane_economics() -> None:
    knobs = DEFAULT_KNOB_MATRIX.knobs_for("opus")
    assert knobs is not None
    # A batch lane exists and is cheaper than interactive (the ~50% discount).
    assert knobs.lanes[LANE_BATCH] < knobs.lanes[LANE_INTERACTIVE]
    # Cache strategies always include "none"; opus prices cache reads/writes so
    # it also offers reuse + warm_up (derived from the price row).
    assert CACHE_NONE in knobs.cache_strategies
    assert CACHE_WARM_UP in knobs.cache_strategies


def test_config_override_extends_defaults_and_changes_hash() -> None:
    override = load_knob_matrix(
        {"opus": {"effort_levels": ["low", "high"], "default_effort": "low"}},
        as_of="2026-07-01",
        revision=2,
    )
    assert override.content_hash() != DEFAULT_KNOB_MATRIX.content_hash()
    knobs = override.knobs_for("opus")
    assert knobs is not None
    assert knobs.effort_levels == ("low", "high")
    # Untouched models still come from the shipped defaults.
    assert override.knobs_for("sonnet") is not None


def test_config_override_rejects_negative_multiplier() -> None:
    with pytest.raises(ValueError, match="negative"):
        load_knob_matrix({"opus": {"lanes": {"batch": -0.5}}})


# ---------------------------------------------------------------------------
# Resolver: determinism, purity, tie-breaks, explicit unknown-model fallback
# ---------------------------------------------------------------------------


def test_resolver_is_deterministic_across_processes() -> None:
    # Two independent resolutions of the same candidate + matrix produce
    # byte-identical canonical JSON (the "two operators" determinism AC).
    first = resolve_knob_selection(candidate=_candidate(), matrix=DEFAULT_KNOB_MATRIX)
    second = resolve_knob_selection(candidate=_candidate(), matrix=DEFAULT_KNOB_MATRIX)
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(second.to_dict(), sort_keys=True)
    assert first.selection_hash == second.selection_hash
    assert first.verify_self_hash()


def test_resolver_reads_no_clock_fs_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # Enforce purity: any clock / filesystem / network access raises, and the
    # resolver must still produce its selection.
    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("resolver touched a clock / filesystem / network")

    monkeypatch.setattr(time, "time", _boom)
    monkeypatch.setattr(builtins, "open", _boom)
    monkeypatch.setattr(socket, "socket", _boom)
    selection = resolve_knob_selection(candidate=_candidate(), matrix=DEFAULT_KNOB_MATRIX)
    assert selection.resolved is True
    assert selection.effort == "high"


def test_batch_lane_selected_only_when_eligible_and_capable() -> None:
    # Batch-eligible on a batch-capable adapter -> batch lane (cheaper).
    on = resolve_knob_selection(candidate=_candidate(batch_eligible=True, adapter="claude"), matrix=DEFAULT_KNOB_MATRIX)
    assert on.lane == LANE_BATCH
    assert on.rate_multiplier < 1.0
    # Batch-eligible on a non-batch adapter -> interactive (never faked).
    off = resolve_knob_selection(candidate=_candidate(batch_eligible=True, adapter="aider"), matrix=DEFAULT_KNOB_MATRIX)
    assert off.lane == LANE_INTERACTIVE
    assert off.rate_multiplier == pytest.approx(1.0)


def test_effort_fallback_is_explicit_when_undeclared() -> None:
    selection = resolve_knob_selection(candidate=_candidate(requested_effort="ludicrous"), matrix=DEFAULT_KNOB_MATRIX)
    knobs = DEFAULT_KNOB_MATRIX.knobs_for("opus")
    assert knobs is not None
    assert selection.effort == knobs.default_effort
    assert selection.reason != "resolved"


def test_unknown_model_resolves_explicit_default_not_silent_fallback() -> None:
    selection = resolve_knob_selection(
        candidate=_candidate(model="totally-unknown-sku-xyz"), matrix=DEFAULT_KNOB_MATRIX
    )
    assert selection.resolved is False
    assert selection.reason == REASON_MODEL_NOT_IN_MATRIX
    # Multiplier 1.0 so the admit/halt outcome is unchanged for unpriced models.
    assert selection.rate_multiplier == pytest.approx(1.0)
    assert selection.verify_self_hash()


def test_unknown_model_leaves_admit_halt_outcome_unchanged() -> None:
    # The decision with an unknown-model knob selection admits/halts identically
    # to one with no selection (same projected cost, only the fingerprint gains
    # the sealed default selection).
    caps = CostCaps(per_run_usd=10.0)
    entries = [_entry(cost_usd=9.5)]
    cand = _candidate(model="totally-unknown-sku-xyz", projected_cost_usd=1.0)
    selection = resolve_knob_selection(candidate=cand, matrix=DEFAULT_KNOB_MATRIX)
    with_knob = decide_dispatch(
        candidate=cand, entries=entries, caps=caps, price_table_hash="sha256:pt", knob_selection=selection
    )
    without = decide_dispatch(candidate=cand, entries=entries, caps=caps, price_table_hash="sha256:pt")
    assert with_knob.admit == without.admit
    assert with_knob.projected_cost_usd == pytest.approx(without.projected_cost_usd)


# ---------------------------------------------------------------------------
# Decision fingerprint: the knob choice is part of the decision identity
# ---------------------------------------------------------------------------


def test_knobs_enter_decision_hash_identically_for_identical_state() -> None:
    caps = CostCaps(per_run_usd=100.0)
    entries = [_entry(cost_usd=1.0)]
    sel1 = resolve_knob_selection(candidate=_candidate(), matrix=DEFAULT_KNOB_MATRIX)
    sel2 = resolve_knob_selection(candidate=_candidate(), matrix=DEFAULT_KNOB_MATRIX)
    d1 = decide_dispatch(
        candidate=_candidate(), entries=entries, caps=caps, price_table_hash="sha256:pt", knob_selection=sel1
    )
    d2 = decide_dispatch(
        candidate=_candidate(), entries=list(entries), caps=caps, price_table_hash="sha256:pt", knob_selection=sel2
    )
    assert d1.decision_hash == d2.decision_hash


def test_knob_change_flips_decision_hash() -> None:
    caps = CostCaps(per_run_usd=100.0)
    entries = [_entry(cost_usd=1.0)]
    base_sel = resolve_knob_selection(candidate=_candidate(requested_effort="high"), matrix=DEFAULT_KNOB_MATRIX)
    changed_sel = resolve_knob_selection(candidate=_candidate(requested_effort="low"), matrix=DEFAULT_KNOB_MATRIX)
    assert base_sel.selection_hash != changed_sel.selection_hash
    base = decide_dispatch(
        candidate=_candidate(), entries=entries, caps=caps, price_table_hash="sha256:pt", knob_selection=base_sel
    )
    changed = decide_dispatch(
        candidate=_candidate(), entries=entries, caps=caps, price_table_hash="sha256:pt", knob_selection=changed_sel
    )
    assert base.decision_hash != changed.decision_hash


def test_decision_without_knob_selection_matches_pre_2519_hash() -> None:
    # Back-compat: a decision taken with no knob selection hashes byte-identically
    # to the pre-#2519 body (the "knob_selection" key is omitted entirely).
    caps = CostCaps(per_run_usd=100.0)
    entries = [_entry(cost_usd=1.0)]
    decision = decide_dispatch(candidate=_candidate(), entries=entries, caps=caps, price_table_hash="sha256:pt")
    assert "knob_selection" not in decision.to_dict()
    assert decision.verify_self_hash()


# ---------------------------------------------------------------------------
# Receipt: sealed knobs verify offline; per-knob-field tamper is named
# ---------------------------------------------------------------------------


def _seal_receipt(tmp_path: Path, *, selection: KnobSelection) -> tuple[Path, Path, str]:
    caps = CostCaps(per_run_usd=100.0)
    entries = [_entry(cost_usd=1.0)]
    decision = decide_dispatch(
        candidate=_candidate(), entries=entries, caps=caps, price_table_hash="sha256:pt", knob_selection=selection
    )
    workdir = tmp_path / "proj"
    lineage_root = workdir / ".sdd" / "lineage"
    build_dispatch_receipt(
        decision=decision, workdir=workdir, lineage_root=lineage_root, hmac_key=_KEY, timestamp=1_762_000_001
    )
    return workdir, lineage_root, decision.decision_hash


def test_sealed_knob_receipt_verifies_offline(tmp_path: Path) -> None:
    selection = resolve_knob_selection(candidate=_candidate(), matrix=DEFAULT_KNOB_MATRIX)
    workdir, lineage_root, decision_hash = _seal_receipt(tmp_path, selection=selection)
    result = verify_dispatch_receipt(
        workdir=workdir, lineage_root=lineage_root, hmac_key=_KEY, decision_hash=decision_hash
    )
    assert result.ok is True
    # The sealed selection is on the receipt.
    payload = json.loads(dispatch_receipt_path(workdir, decision_hash).read_text(encoding="utf-8"))
    assert payload["knob_selection"]["lane"] in {LANE_INTERACTIVE, LANE_BATCH}
    assert payload["knob_selection"]["selection_hash"] == selection.selection_hash


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("effort", "low"),
        ("lane", "priority"),
        ("cache_strategy", "warm_up"),
        ("rate_multiplier", 0.25),
    ],
)
def test_tampering_any_knob_field_fails_verification_naming_it(
    tmp_path: Path, field_name: str, tampered_value: object
) -> None:
    selection = resolve_knob_selection(
        candidate=_candidate(batch_eligible=True, adapter="claude"), matrix=DEFAULT_KNOB_MATRIX
    )
    workdir, lineage_root, decision_hash = _seal_receipt(tmp_path, selection=selection)
    path = dispatch_receipt_path(workdir, decision_hash)
    forged = json.loads(path.read_text(encoding="utf-8"))
    assert forged["knob_selection"][field_name] != tampered_value
    forged["knob_selection"][field_name] = tampered_value
    path.write_text(json.dumps(forged), encoding="utf-8")

    result = verify_dispatch_receipt(
        workdir=workdir, lineage_root=lineage_root, hmac_key=_KEY, decision_hash=decision_hash
    )
    assert result.ok is False
    assert field_name in result.reason


def test_all_knob_fields_are_field_addressed() -> None:
    # Every named knob field carries its own sealed digest.
    selection = resolve_knob_selection(candidate=_candidate(), matrix=DEFAULT_KNOB_MATRIX)
    assert selection.field_digests is not None
    assert set(selection.field_digests) == set(KNOB_FIELDS)
    assert selection.first_field_digest_mismatch() is None


# ---------------------------------------------------------------------------
# Journal: knob selections fold into the Merkle head; a flip diverges by index
# ---------------------------------------------------------------------------


def _record_run(sdd: Path, *, run_id: str, effort: str) -> str:
    journal = EventJournal(run_id, sdd)
    journal.record("task_claimed", task_id="t1")
    selection = resolve_knob_selection(candidate=_candidate(requested_effort=effort), matrix=DEFAULT_KNOB_MATRIX)
    record_dispatch_knob_selection(
        journal,
        task_id="t1",
        run_id=run_id,
        selection_hash=selection.selection_hash,
        effort=selection.effort,
        lane=selection.lane,
        cache_strategy=selection.cache_strategy,
        rate_multiplier=selection.rate_multiplier,
        resolved=selection.resolved,
        reason=selection.reason,
    )
    journal.record("task_done", task_id="t1")
    return journal.head()


def test_identical_runs_reproduce_journal_head(tmp_path: Path) -> None:
    head_a = _record_run(tmp_path / "a", run_id="run", effort="high")
    head_b = _record_run(tmp_path / "b", run_id="run", effort="high")
    assert head_a == head_b


def test_forced_knob_change_diverges_at_exact_step(tmp_path: Path) -> None:
    sdd = tmp_path / "sdd"
    _record_run(sdd, run_id="run", effort="high")
    journal_path = sdd / "runs" / "run" / "journal.jsonl"
    rows = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    # The knob-selection event is the second row (index 1).
    knob_idx = next(i for i, r in enumerate(rows) if r.get("event") == DISPATCH_KNOB_SELECTION_EVENT)
    rows[knob_idx]["effort"] = "low"  # forced knob change, hash not recomputed
    journal_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    result = EventJournal("run", sdd).verify()
    assert result.ok is False
    assert result.divergent_index == knob_idx


# ---------------------------------------------------------------------------
# Candidate costing: the resolved lane multiplier reaches the projection
# ---------------------------------------------------------------------------


def test_build_candidates_applies_lane_multiplier() -> None:
    batches = [[_Task(id="t1", model="opus", adapter="claude", is_batch=True)]]
    candidates = build_dispatch_candidates(
        batches,
        cost_estimates={"t1": 2.0},
        run_id="r1",
        day_key=_day_key(1_762_000_000.0),
        knob_matrix=DEFAULT_KNOB_MATRIX,
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.knob_selection is not None
    assert cand.knob_selection.lane == LANE_BATCH
    # Base estimate 2.0 * batch multiplier (< 1) -> lower projection.
    assert cand.projected_cost_usd < 2.0
    assert cand.projected_cost_usd == pytest.approx(2.0 * cand.knob_selection.rate_multiplier)


def test_build_candidates_without_matrix_is_unchanged() -> None:
    batches = [[_Task(id="t1", model="opus")]]
    candidates = build_dispatch_candidates(
        batches, cost_estimates={"t1": 2.0}, run_id="r1", day_key=_day_key(1_762_000_000.0)
    )
    assert candidates[0].projected_cost_usd == pytest.approx(2.0)
    assert candidates[0].knob_selection is None


def test_lane_choice_names_the_knob_in_halt_receipt(tmp_path: Path) -> None:
    # A candidate whose interactive projection would breach a cap but whose
    # batch lane keeps it under: the resolved selection is sealed into the
    # decision so a verifier can attribute the outcome to the lane knob.
    day = _day_key(1_762_000_000.0)
    batches = [[_Task(id="t1", model="opus", adapter="claude", is_batch=True)]]
    candidates = build_dispatch_candidates(
        batches, cost_estimates={"t1": 2.0}, run_id="r1", day_key=day, knob_matrix=DEFAULT_KNOB_MATRIX
    )
    caps = CostCaps(per_run_usd=1.5)  # 2.0 interactive would breach; batch (1.0) does not
    outcome = evaluate_run_dispatch(
        candidates=candidates, entries=[], caps=caps, price_table_hash="sha256:pt", now_ts=1_762_000_000.0
    )
    assert outcome.halt is None  # the batch lane multiplier kept it admitted
    admitted = outcome.admitted[0]
    assert admitted.knob_selection is not None
    assert admitted.knob_selection.lane == LANE_BATCH


# ---------------------------------------------------------------------------
# Staleness advisory: non-blocking doctor hint
# ---------------------------------------------------------------------------


def test_staleness_advisory_is_nonblocking_and_never_raises() -> None:
    fresh = knob_matrix_staleness(DEFAULT_KNOB_MATRIX, now_iso=DEFAULT_KNOB_MATRIX.as_of)
    assert fresh.stale is False
    stale = knob_matrix_staleness(DEFAULT_KNOB_MATRIX, now_iso="2099-01-01")
    assert stale.stale is True
    # A malformed as_of is treated as stale (fail-visible), never raises.
    bad = knob_matrix_staleness(KnobMatrix(models={}, as_of="not-a-date"), now_iso="2026-07-16")
    assert bad.stale is True


def test_resolve_knob_matrix_defaults_without_config() -> None:
    assert resolve_knob_matrix(None).content_hash() == DEFAULT_KNOB_MATRIX.content_hash()

    @dataclass
    class _Knobs:
        models: dict[str, object] = field(default_factory=dict)

    @dataclass
    class _Policy:
        knobs: _Knobs = field(default_factory=_Knobs)

    # Empty knobs block -> shipped defaults unchanged.
    assert resolve_knob_matrix(_Policy()).content_hash() == DEFAULT_KNOB_MATRIX.content_hash()
