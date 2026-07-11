"""Governance projection tests (issue #2309).

Each test maps to an acceptance criterion:

* AC1 -- every access decision produces a signed, chained record carrying
  ``inputs_hash`` and a journal anchor.
* AC2 -- ``verify_governance`` recomputes all access and budget verdicts from
  the chain and matches the recorded verdicts; a tamper fails it.
* AC3 -- a budget breach writes a signed refusal and blocks the action.
* AC4 -- per-seat spend is recomputable from the cost ledger rather than read
  from a mutable counter.
* AC5 -- two replays of a governance-gated run produce identical decision
  records.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.cost.spend_ledger import CallTags, SpendLedger
from bernstein.core.security.governance import (
    GOVERNANCE_ACTOR,
    BudgetRefused,
    GovernanceDecision,
    RoleBindings,
    check_budget_decision,
    decide_access,
    decisions_dir,
    read_decisions,
    resolve_role,
    seat_spend,
    verify_governance,
)

_KEY = b"0" * 32


def _bindings(key: bytes) -> RoleBindings:
    return RoleBindings(
        group_to_role={"eng": "operator", "admins": "admin", "readers": "viewer"},
        role_permissions={
            "admin": ("tasks:write", "config:write", "costs:read"),
            "operator": ("tasks:write", "costs:read"),
            "viewer": ("costs:read",),
        },
    ).sign(key)


def _lineage_root(tmp_path: Path) -> Path:
    return tmp_path / ".sdd" / "lineage"


# --------------------------------------------------------------------------- AC1


def test_access_decision_produces_signed_chained_record(tmp_path: Path) -> None:
    decision = decide_access(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        subject="alice",
        idp_groups=("eng",),
        action="tasks:write",
        bindings=_bindings(_KEY),
        now=1000,
    )
    assert decision.verdict == "allow"
    assert decision.subject == "alice"
    assert decision.action == "tasks:write"
    assert decision.inputs_hash.startswith("sha256:")
    # journal anchor is the spine entry hash over the record bytes
    assert decision.journal_entry_hash
    # persisted for offline verify
    records = read_decisions(_lineage_root(tmp_path), "run-1")
    assert len(records) == 1
    assert records[0].journal_entry_hash == decision.journal_entry_hash


def test_denied_action_writes_signed_denial_record(tmp_path: Path) -> None:
    decision = decide_access(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        subject="bob",
        idp_groups=("readers",),
        action="tasks:write",
        bindings=_bindings(_KEY),
        now=1000,
    )
    assert decision.verdict == "deny"
    # a denial is still a signed, anchored record
    assert decision.journal_entry_hash
    records = read_decisions(_lineage_root(tmp_path), "run-1")
    assert [r.verdict for r in records] == ["deny"]


def test_unmapped_group_resolves_to_no_role_and_denies(tmp_path: Path) -> None:
    bindings = _bindings(_KEY)
    assert resolve_role(("nobody",), bindings) == ""
    decision = decide_access(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        subject="carol",
        idp_groups=("nobody",),
        action="costs:read",
        bindings=bindings,
        now=1000,
    )
    assert decision.verdict == "deny"


def test_highest_privilege_group_wins(tmp_path: Path) -> None:
    # A subject in both readers and admins resolves to admin (highest wins).
    bindings = _bindings(_KEY)
    assert resolve_role(("readers", "admins"), bindings) == "admin"


# --------------------------------------------------------------------------- AC2


def test_verify_governance_recomputes_and_matches(tmp_path: Path) -> None:
    bindings = _bindings(_KEY)
    decide_access(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        subject="alice",
        idp_groups=("eng",),
        action="tasks:write",
        bindings=bindings,
        now=1000,
    )
    decide_access(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        subject="bob",
        idp_groups=("readers",),
        action="config:write",
        bindings=bindings,
        now=1001,
    )
    result = verify_governance(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        bindings=bindings,
    )
    assert result.ok, result.reason
    assert result.checked == 2


def test_verify_governance_includes_budget_decisions(tmp_path: Path) -> None:
    bindings = _bindings(_KEY)
    ledger_path = tmp_path / ".sdd" / "cost" / "ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = SpendLedger(path=ledger_path)
    ledger.record(tags=CallTags(agent_id="alice"), model="haiku", cost_usd=1.0)

    decide_access(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        subject="alice",
        idp_groups=("eng",),
        action="tasks:write",
        bindings=bindings,
        now=1000,
    )
    check_budget_decision(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        subject="alice",
        cap_usd=10.0,
        next_cost_usd=2.0,
        ledger_path=ledger_path,
        now=1001,
    )
    result = verify_governance(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        bindings=bindings,
        ledger_path=ledger_path,
    )
    assert result.ok, result.reason
    assert result.checked == 2


def test_verify_fails_when_verdict_tampered(tmp_path: Path) -> None:
    bindings = _bindings(_KEY)
    decide_access(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        subject="alice",
        idp_groups=("eng",),
        action="tasks:write",
        bindings=bindings,
        now=1000,
    )
    # Flip the recorded verdict on disk (allow -> deny) without re-anchoring.
    out_dir = decisions_dir(_lineage_root(tmp_path), "run-1")
    (target,) = list(out_dir.glob("*.json"))
    raw = target.read_text(encoding="utf-8")
    target.write_text(raw.replace('"allow"', '"deny"'), encoding="utf-8")
    result = verify_governance(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        bindings=bindings,
    )
    assert not result.ok


def test_verify_fails_when_recomputed_verdict_differs(tmp_path: Path) -> None:
    # A record whose recorded verdict disagrees with a fresh recompute under
    # the presented bindings must fail (a permission was widened after the fact).
    bindings = _bindings(_KEY)
    decide_access(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        subject="bob",
        idp_groups=("readers",),
        action="tasks:write",
        bindings=bindings,
        now=1000,
    )
    # Present different bindings under which readers CAN write.
    widened = RoleBindings(
        group_to_role={"readers": "viewer"},
        role_permissions={"viewer": ("tasks:write",)},
    ).sign(_KEY)
    result = verify_governance(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        bindings=widened,
    )
    assert not result.ok


def test_verify_empty_run_is_not_ok(tmp_path: Path) -> None:
    result = verify_governance(
        run_id="run-empty",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        bindings=_bindings(_KEY),
    )
    assert not result.ok
    assert result.checked == 0


# --------------------------------------------------------------------------- AC3


def test_budget_breach_writes_signed_refusal_and_blocks(tmp_path: Path) -> None:
    ledger_path = tmp_path / ".sdd" / "cost" / "ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = SpendLedger(path=ledger_path)
    ledger.record(tags=CallTags(agent_id="alice"), model="haiku", cost_usd=9.0)

    with pytest.raises(BudgetRefused):
        check_budget_decision(
            run_id="run-1",
            lineage_root=_lineage_root(tmp_path),
            hmac_key=_KEY,
            subject="alice",
            cap_usd=10.0,
            next_cost_usd=5.0,
            ledger_path=ledger_path,
            now=1000,
        )
    # the refusal is a signed, anchored record
    records = read_decisions(_lineage_root(tmp_path), "run-1")
    assert [r.verdict for r in records] == ["refuse"]
    assert records[0].action == "budget"


def test_budget_within_cap_allows(tmp_path: Path) -> None:
    ledger_path = tmp_path / ".sdd" / "cost" / "ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = SpendLedger(path=ledger_path)
    ledger.record(tags=CallTags(agent_id="alice"), model="haiku", cost_usd=1.0)

    decision = check_budget_decision(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        subject="alice",
        cap_usd=10.0,
        next_cost_usd=2.0,
        ledger_path=ledger_path,
        now=1000,
    )
    assert decision.verdict == "allow"
    assert decision.action == "budget"


def test_budget_verify_matches_recomputed_from_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / ".sdd" / "cost" / "ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = SpendLedger(path=ledger_path)
    ledger.record(tags=CallTags(agent_id="alice"), model="haiku", cost_usd=9.0)

    with pytest.raises(BudgetRefused):
        check_budget_decision(
            run_id="run-1",
            lineage_root=_lineage_root(tmp_path),
            hmac_key=_KEY,
            subject="alice",
            cap_usd=10.0,
            next_cost_usd=5.0,
            ledger_path=ledger_path,
            now=1000,
        )
    result = verify_governance(
        run_id="run-1",
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        bindings=_bindings(_KEY),
        ledger_path=ledger_path,
    )
    assert result.ok, result.reason
    assert result.checked == 1


# --------------------------------------------------------------------------- AC4


def test_seat_spend_recomputed_from_ledger_not_counter(tmp_path: Path) -> None:
    ledger_path = tmp_path / ".sdd" / "cost" / "ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = SpendLedger(path=ledger_path)
    ledger.record(tags=CallTags(agent_id="alice"), model="haiku", cost_usd=2.0)
    ledger.record(tags=CallTags(agent_id="bob"), model="haiku", cost_usd=3.0)
    ledger.record(tags=CallTags(agent_id="alice"), model="haiku", cost_usd=1.5)

    # Recompute from disk (a fresh reader with no in-process counter).
    assert seat_spend(ledger_path, "alice") == pytest.approx(3.5)
    assert seat_spend(ledger_path, "bob") == pytest.approx(3.0)
    assert seat_spend(ledger_path, "nobody") == 0.0


def test_seat_spend_missing_ledger_is_zero(tmp_path: Path) -> None:
    assert seat_spend(tmp_path / "absent.jsonl", "alice") == 0.0


def test_seat_spend_dimension_role(tmp_path: Path) -> None:
    ledger_path = tmp_path / ".sdd" / "cost" / "ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = SpendLedger(path=ledger_path)
    ledger.record(tags=CallTags(role="reviewer"), model="haiku", cost_usd=4.0)
    ledger.record(tags=CallTags(role="planner"), model="haiku", cost_usd=1.0)
    assert seat_spend(ledger_path, "reviewer", dimension="role") == pytest.approx(4.0)


# --------------------------------------------------------------------------- AC5


def test_two_replays_produce_identical_decision_records(tmp_path: Path) -> None:
    bindings = _bindings(_KEY)

    def _run(root: Path) -> GovernanceDecision:
        return decide_access(
            run_id="run-1",
            lineage_root=root / ".sdd" / "lineage",
            hmac_key=_KEY,
            subject="alice",
            idp_groups=("eng",),
            action="tasks:write",
            bindings=bindings,
            now=1000,
        )

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    d1 = _run(a)
    d2 = _run(b)
    assert d1.to_dict() == d2.to_dict()
    # byte-identical persisted records
    ra = _one_record(a).read_bytes()
    rb = _one_record(b).read_bytes()
    assert ra == rb


def _one_record(root: Path) -> Path:
    out_dir = decisions_dir(root / ".sdd" / "lineage", "run-1")
    (one,) = list(out_dir.glob("*.json"))
    return one


def test_actor_constant_is_stable() -> None:
    assert GOVERNANCE_ACTOR == "bernstein.governance"
