"""Tests for the deterministic subagent-delegation execution layer (issue #2308).

The scheduler owns a deterministic outer plan; each leaf delegates mechanical
execution to a native subagent (Claude Code, Codex, ...) with per-agent
model/effort/tools/background/batch settings. The native result is
schema-validated at the worker boundary and anchored into the run event
journal, so the cross-worker DAG replays byte-identically even though inner
execution is stochastic. Non-interactive fan-out is dispatched on the batch
tier and its discount is recorded in the spend ledger; prompt-cache
breakpoints are attributed as cache reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.agents.subagent_delegation import (
    BATCH_TIER_DISCOUNT,
    DispatchNode,
    NativeResultRejected,
    OuterPlan,
    delegate_plan,
    dispatch_node,
)
from bernstein.core.cost.spend_ledger import SpendLedger
from bernstein.core.replay.journal import EventJournal


def _journal(tmp_path: Path, run_id: str = "run-1") -> EventJournal:
    return EventJournal(run_id=run_id, sdd_dir=tmp_path / ".sdd")


_AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string"},
        "findings": {"type": "integer"},
    },
    "required": ["verdict", "findings"],
}


def _node(name: str = "audit-0", *, batch: bool = False) -> DispatchNode:
    return DispatchNode(
        name=name,
        target="claude",
        model="claude-sonnet-4",
        effort="high",
        tools=("Read", "Grep"),
        prompt="Audit module X for regressions.",
        result_schema=_AUDIT_SCHEMA,
        batch=batch,
    )


# ---------------------------------------------------------------------------
# AC1 - schema validation at the boundary
# ---------------------------------------------------------------------------


def test_dispatch_validates_native_result_against_schema(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    result = dispatch_node(
        _node(),
        native_result={"verdict": "clean", "findings": 0},
        journal=journal,
    )
    assert result.validated is True
    assert result.payload == {"verdict": "clean", "findings": 0}
    # A journal event was anchored for the delegation.
    assert journal.event_count() == 1


def test_dispatch_rejects_extra_keys_from_native_result(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(NativeResultRejected) as exc:
        dispatch_node(
            _node(),
            native_result={"verdict": "clean", "findings": 0, "hallucinated": True},
            journal=journal,
        )
    assert "hallucinated" in str(exc.value)


def test_dispatch_rejects_missing_required_field(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(NativeResultRejected):
        dispatch_node(
            _node(),
            native_result={"verdict": "clean"},
            journal=journal,
        )


# ---------------------------------------------------------------------------
# AC2 - deterministic cross-worker replay even when inner execution varies
# ---------------------------------------------------------------------------


def test_outer_plan_hash_is_deterministic() -> None:
    plan_a = OuterPlan(nodes=(_node("a"), _node("b")))
    plan_b = OuterPlan(nodes=(_node("a"), _node("b")))
    assert plan_a.plan_hash() == plan_b.plan_hash()


def test_outer_plan_hash_changes_with_node_order() -> None:
    plan_a = OuterPlan(nodes=(_node("a"), _node("b")))
    plan_b = OuterPlan(nodes=(_node("b"), _node("a")))
    assert plan_a.plan_hash() != plan_b.plan_hash()


def test_dag_replays_byte_identically_when_inner_result_varies(tmp_path: Path) -> None:
    plan = OuterPlan(nodes=(_node("audit-0"), _node("audit-1")))

    # Run 1: one set of (stochastic) native payloads.
    j1 = _journal(tmp_path / "r1", run_id="r1")
    delegate_plan(
        plan,
        journal=j1,
        native_results={
            "audit-0": {"verdict": "clean", "findings": 0},
            "audit-1": {"verdict": "dirty", "findings": 3},
        },
    )
    # Run 2: DIFFERENT native payloads for the same plan.
    j2 = _journal(tmp_path / "r2", run_id="r2")
    delegate_plan(
        plan,
        journal=j2,
        native_results={
            "audit-0": {"verdict": "dirty", "findings": 9},
            "audit-1": {"verdict": "clean", "findings": 0},
        },
    )

    # The outer DAG identity - the sequence of delegation-node hashes - is a
    # pure function of the plan, independent of the stochastic inner payloads.
    assert _delegation_node_hashes(j1) == _delegation_node_hashes(j2)
    # But the anchored result content hashes differ (inner execution varied).
    assert _result_content_hashes(j1) != _result_content_hashes(j2)
    # Both chains verify.
    assert j1.verify().ok
    assert j2.verify().ok


def _delegation_node_hashes(journal: EventJournal) -> list[str]:
    from bernstein.core.replay.journal import load_events

    return [str(e["node_hash"]) for e in load_events(journal.path) if e.get("event") == "subagent.delegation"]


def _result_content_hashes(journal: EventJournal) -> list[str]:
    from bernstein.core.replay.journal import load_events

    return [str(e["result_content_hash"]) for e in load_events(journal.path) if e.get("event") == "subagent.delegation"]


# ---------------------------------------------------------------------------
# AC3 - batch tier discount recorded in the spend ledger
# ---------------------------------------------------------------------------


def test_batch_fan_out_records_discount_in_ledger(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    ledger = SpendLedger(path=tmp_path / "cost" / "ledger.jsonl", run_id="run-1")
    dispatch_node(
        _node(batch=True),
        native_result={"verdict": "clean", "findings": 0},
        journal=journal,
        ledger=ledger,
        undiscounted_cost_usd=1.00,
        input_tokens=1000,
        output_tokens=200,
    )
    entries = SpendLedger.load_entries(tmp_path / "cost" / "ledger.jsonl")
    assert len(entries) == 1
    entry = entries[0]
    # The batch tier applied a discount: recorded cost is below the
    # undiscounted cost by exactly the batch factor.
    assert entry.cost_usd == pytest.approx(1.00 * (1.0 - BATCH_TIER_DISCOUNT))
    assert entry.tags.get("tier") == "batch"


def test_interactive_fan_out_records_no_discount(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    ledger = SpendLedger(path=tmp_path / "cost" / "ledger.jsonl", run_id="run-1")
    dispatch_node(
        _node(batch=False),
        native_result={"verdict": "clean", "findings": 0},
        journal=journal,
        ledger=ledger,
        undiscounted_cost_usd=1.00,
        input_tokens=1000,
        output_tokens=200,
    )
    entry = SpendLedger.load_entries(tmp_path / "cost" / "ledger.jsonl")[0]
    assert entry.cost_usd == pytest.approx(1.00)
    assert entry.tags.get("tier") == "interactive"


# ---------------------------------------------------------------------------
# AC4 - prompt-cache breakpoint attribution
# ---------------------------------------------------------------------------


def test_cache_hits_attributed_in_ledger(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    ledger = SpendLedger(path=tmp_path / "cost" / "ledger.jsonl", run_id="run-1")
    dispatch_node(
        _node(),
        native_result={"verdict": "clean", "findings": 0},
        journal=journal,
        ledger=ledger,
        undiscounted_cost_usd=0.50,
        input_tokens=1000,
        output_tokens=100,
        cache_read_tokens=800,
    )
    entry = SpendLedger.load_entries(tmp_path / "cost" / "ledger.jsonl")[0]
    assert entry.cache_read_tokens == 800


# ---------------------------------------------------------------------------
# Audit-chain anchoring
# ---------------------------------------------------------------------------


def test_delegation_records_audit_chain_event(tmp_path: Path) -> None:
    from bernstein.core.security.audit_chain import (
        EVENT_SUBAGENT_DELEGATION,
        AuditChainStore,
    )

    journal = _journal(tmp_path)
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    result = dispatch_node(
        _node(),
        native_result={"verdict": "clean", "findings": 0},
        journal=journal,
        chain=chain,
    )
    events = chain.query(event_type=EVENT_SUBAGENT_DELEGATION)
    assert len(events) == 1
    ev = events[0]
    assert ev.details["node_hash"] == result.node_hash
    assert ev.details["result_content_hash"] == result.result_content_hash
    assert "prev_chain_digest" in ev.details
    ok, errors = chain.verify()
    assert ok, errors
