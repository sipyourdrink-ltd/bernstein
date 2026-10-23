"""Property: every collusion flag is attributable, blocks admission, is
recorded in the signed merge receipt, and evidence gaps never yield a clean
verdict.

A flag that doesn't name the invariant and both tasks can't be acted on or
appealed; a "pass" over tasks with no footprints — or over an empty
candidate set — would be the bypass the pipeline's status vocabulary exists
to prevent; a refusal not bound into the signed receipt preimage wouldn't
survive an audit.
"""

from bernstein.core.lineage.dependency import ChangeFact, TaskEffects
from bernstein.core.quality.collusion import (
    INV_GUARDED_SYMBOL,
    check_pair,
    cross_task_check,
)
from bernstein.core.quality.collusion_gate import (
    GATE_NAME,
    admission_decision,
    run_cross_task_gate,
)
from bernstein.core.quality.merge_receipt import (
    DECISION_ADMIT,
    DECISION_REFUSE,
    MERGE_SCHEMA_VERSION,
    MergeAdmissionReceipt,
)


def _split_guard_pair() -> tuple[TaskEffects, TaskEffects]:
    a = TaskEffects(
        task_id="task-remove-defn",
        facts=(
            ChangeFact(
                kind="remove-definition",
                path="src/crypto/verify.py",
                symbol="verify_signature",
            ),
        ),
        writes=frozenset({"src/crypto/verify.py"}),
    )
    b = TaskEffects(
        task_id="task-remove-call",
        facts=(
            ChangeFact(
                kind="remove-reference",
                path="src/api/webhooks.py",
                symbol="verify_signature",
            ),
        ),
        writes=frozenset({"src/api/webhooks.py"}),
        reads=frozenset({"src/crypto/verify.py"}),
    )
    return a, b


def _receipt(collusion: dict | None) -> MergeAdmissionReceipt:
    return MergeAdmissionReceipt(
        head_sha="a" * 40,
        merge_base_sha="b" * 40,
        required_context_ids=(),
        gate_results_hash="sha256:gate",
        ruleset_hash="sha256:rules",
        review_receipt_id="",
        journal_head="",
        decision=DECISION_REFUSE if collusion else DECISION_ADMIT,
        authority="autonomous",
        timestamp=0,
        collusion=collusion,
    )


def test_flags_name_invariant_and_both_tasks_and_block_admission():
    a, b = _split_guard_pair()

    flags = check_pair(a, b)
    assert flags, "split removal of a guarded symbol must be flagged"
    for flag in flags:
        assert flag.invariant == INV_GUARDED_SYMBOL
        assert {flag.task_a, flag.task_b} == {a.task_id, b.task_id}

    # order-independent: swapping admission order must not lose the flag
    swapped = check_pair(b, a)
    assert {(f.invariant, f.task_a, f.task_b) for f in swapped} == {(f.invariant, f.task_a, f.task_b) for f in flags}

    verdict = cross_task_check([a, b])
    assert not verdict.clean

    admission = run_cross_task_gate([a, b])
    gate = admission.gate_result
    assert gate.name == GATE_NAME
    assert gate.status == "fail"
    assert gate.blocked is True
    assert admission.admitted is False
    assert gate.reason is None
    assert admission_decision(admission) == DECISION_REFUSE

    section = admission.receipt_section()
    assert section["check"] == "cross-task-collusion"
    assert section["gate_status"] == "fail"
    assert section["admitted"] is False
    assert section["checked_pairs"] == 1
    assert section["coupled_pairs"] == 1
    assert section["void_tasks"] == []
    assert section["flags"] and section["flags"][0]["invariant"] == INV_GUARDED_SYMBOL
    assert {section["flags"][0]["task_a"], section["flags"][0]["task_b"]} == {
        a.task_id,
        b.task_id,
    }
    # the gate's scope attests exactly what was examined
    assert gate.scope is not None
    assert gate.scope.checked == ("task-remove-call+task-remove-defn",)
    assert gate.scope.confidence == "high"


def test_receipt_scope_covers_evidenced_paths_only():
    a, b = _split_guard_pair()
    void = TaskEffects(task_id="opaque-task")
    admission = run_cross_task_gate([a, b, void])
    scope = admission.to_receipt_scope()
    assert scope.oracle == "cross_task_collusion"
    assert scope.checked == ("src/api/webhooks.py", "src/crypto/verify.py")
    assert scope.skipped == ()


def test_clean_merge_records_check_and_passes():
    a, _ = _split_guard_pair()
    admission = run_cross_task_gate([a])
    gate = admission.gate_result
    assert gate.status == "pass"
    assert gate.blocked is False
    assert admission.admitted is True
    assert admission_decision(admission) == DECISION_ADMIT
    assert gate.scope is not None and gate.scope.checked == ()
    assert gate.scope.confidence == "high"
    assert admission.receipt_section()["flags"] == []


def test_all_void_candidates_are_inconclusive_not_pass():
    void = TaskEffects(task_id="opaque-task")
    admission = run_cross_task_gate([void])
    gate = admission.gate_result
    assert gate.status == "inconclusive"
    assert gate.reason == "evidence-missing"
    assert gate.blocked is True
    assert admission.admitted is False
    assert admission_decision(admission) == DECISION_REFUSE
    assert gate.scope is not None and gate.scope.confidence == "none"
    assert admission.receipt_section()["void_tasks"] == ["opaque-task"]


def test_empty_candidate_set_is_inconclusive_not_pass():
    # an empty set has no evidence at all; "pass" over it would be the
    # bypass the status vocabulary exists to prevent
    admission = run_cross_task_gate([])
    gate = admission.gate_result
    assert gate.status == "inconclusive"
    assert gate.reason == "evidence-missing"
    assert gate.blocked is True
    assert admission.admitted is False
    assert admission_decision(admission) == DECISION_REFUSE
    assert gate.scope is not None and gate.scope.confidence == "none"
    section = admission.receipt_section()
    assert section["void_tasks"] == []
    assert section["flags"] == []
    assert section["gate_status"] == "inconclusive"


def test_partial_evidence_downgrades_scope_and_names_the_gap():
    evidenced, other = _split_guard_pair()
    void = TaskEffects(task_id="opaque-task")
    admission = run_cross_task_gate([evidenced, other, void])
    gate = admission.gate_result
    # a set containing any unrecorded candidate can never yield a clean
    # verdict — but proven flags are still recorded for the appeal
    assert gate.status == "inconclusive"
    assert gate.reason == "evidence-missing"
    assert gate.blocked is True
    assert admission.admitted is False
    assert admission_decision(admission) == DECISION_REFUSE
    assert gate.scope is not None
    assert gate.scope.confidence == "partial"
    assert gate.scope.cannot_check == ("opaque-task (no effect footprints recorded)",)
    section = admission.receipt_section()
    assert section["gate_status"] == "inconclusive"
    assert section["flags"] and section["flags"][0]["invariant"] == INV_GUARDED_SYMBOL


def test_merge_receipt_binding_carries_collusion_record():
    admission = run_cross_task_gate(list(_split_guard_pair()))
    receipt = _receipt(admission.receipt_section())
    assert receipt.schema_version == MERGE_SCHEMA_VERSION == 3

    # the record is inside the signed preimage, and binds the flags
    binding = receipt.to_canonical_bytes()
    assert b'"collusion"' in binding
    assert INV_GUARDED_SYMBOL.encode() in binding

    # round-trips through the serialised form
    loaded = MergeAdmissionReceipt.from_dict(receipt.to_dict())
    assert loaded.collusion == receipt.collusion

    # a different record changes the signed preimage
    other = _receipt({**admission.receipt_section(), "admitted": True})
    assert other.to_canonical_bytes() != binding

    # v2 receipts (pre-collusion) load unchanged, collusion=None
    v2_row = receipt.to_dict() | {"v": 2}
    v2 = MergeAdmissionReceipt.from_dict(v2_row)
    assert v2.collusion is None
    assert b'"collusion"' not in v2.to_canonical_bytes()


def test_receipt_without_collusion_binds_null_at_v3():
    receipt = _receipt(None)
    assert b'"collusion":null' in receipt.to_canonical_bytes()
    assert MergeAdmissionReceipt.from_dict(receipt.to_dict()).collusion is None
