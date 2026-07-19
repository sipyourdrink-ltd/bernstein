"""Automation bridge: signed trigger receipts and chain-anchored status proofs (#2512).

Covers the acceptance criteria directly:

* an admitted trigger mints a receipt that verifies offline against the chain,
  and flipping any byte of the receipt or of the original payload fails it;
* an unauthenticated or replayed trigger is refused and the refusal is itself a
  signed, chain-anchored receipt;
* identical payload bytes produce an identical payload digest and an identical
  canonical task-graph projection;
* a status callback altered in transit fails verification and the verifier
  reports the chain's actual recorded status for the run;
* callbacks re-sent after a transient failure carry byte-identical envelopes;
* the proof envelope is additive, so existing payload consumers keep parsing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.trigger_sources.receipt import (
    PROOF_ENVELOPE_KEY,
    REFUSAL_REPLAYED_TRIGGER,
    REFUSAL_UNAUTHENTICATED,
    TRIGGER_OUTCOME_ADMITTED,
    TRIGGER_OUTCOME_REFUSED,
    RefusalBudget,
    StatusProof,
    TriggerReceipt,
    admit_trigger,
    compute_payload_digest,
    emit_status_proof,
    project_task_graph,
    verify_receipt_document,
    wrap_status_payload,
)

_BODY = json.dumps({"title": "Rotate the deploy key", "description": "quarterly rotation"}).encode()


@pytest.fixture()
def bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Return an isolated bridge root, audit dir, and HMAC key."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    return {
        "root": tmp_path / "automation-bridge",
        "audit_dir": tmp_path / "audit",
        "hmac_key": load_or_create_audit_key(tmp_path / "audit.key"),
    }


def _admit(bridge: dict[str, object], *, trigger_id: str = "n8n-exec-1", body: bytes = _BODY, **kw: object):
    return admit_trigger(
        root=bridge["root"],
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
        platform=kw.pop("platform", "n8n"),
        request_path=kw.pop("request_path", "/webhook"),
        trigger_id=trigger_id,
        body=body,
        scope=kw.pop("scope", "task:create"),
        timestamp=kw.pop("timestamp", 1_700_000_000),
        authenticated=kw.pop("authenticated", True),
        refusal_reason=kw.pop("refusal_reason", ""),
    )


def _archive_chain(bridge: dict[str, object]) -> None:
    """Roll every live audit segment into the compressed ``archive/`` tree.

    Models routine retention: after the window the day's ``<date>.jsonl`` is
    gzip-compressed to ``archive/<date>.jsonl.gz`` and the live file removed.
    A negative retention window forces today's segment across the boundary so
    the test does not have to wait for the wall clock.
    """
    from bernstein.core.security.audit import AuditLog, RetentionPolicy

    audit_dir = bridge["audit_dir"]
    log = AuditLog(audit_dir=audit_dir, key=bridge["hmac_key"])
    result = log.archive(RetentionPolicy(retention_days=-1))
    assert result.archived, "expected the live segment to be archived"
    assert not list(audit_dir.glob("*.jsonl")), "live segment should be gone after archival"


# ---------------------------------------------------------------------------
# Inbound: signed trigger receipts
# ---------------------------------------------------------------------------


def test_admitted_trigger_mints_receipt_that_verifies_offline(bridge: dict[str, object]) -> None:
    """An admitted trigger returns a receipt that re-verifies against the chain."""
    admission = _admit(bridge)

    assert admission.admitted is True
    receipt = admission.receipt
    assert receipt.outcome == TRIGGER_OUTCOME_ADMITTED
    assert receipt.signature
    assert receipt.signer_public_key_pem
    assert receipt.chain_entry_hash

    result = verify_receipt_document(
        receipt.to_dict(),
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
        body=_BODY,
    )
    assert result.ok, result.reason
    assert result.kind == "trigger"


def test_flipping_a_receipt_byte_fails_verification(bridge: dict[str, object]) -> None:
    """Editing any signed field of the stored receipt breaks verification."""
    receipt = _admit(bridge).receipt

    tampered = receipt.to_dict()
    tampered["scope"] = "task:create task:delete"
    result = verify_receipt_document(
        tampered,
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert not result.ok
    assert "signature" in result.reason


def test_flipping_the_original_payload_fails_verification(bridge: dict[str, object]) -> None:
    """A payload that no longer digests to the receipt's value fails."""
    receipt = _admit(bridge).receipt

    result = verify_receipt_document(
        receipt.to_dict(),
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
        body=_BODY.replace(b"quarterly", b"emergency"),
    )
    assert not result.ok
    assert "payload digest" in result.reason


def test_unauthenticated_trigger_is_refused_with_a_signed_receipt(bridge: dict[str, object]) -> None:
    """A trigger that failed authentication produces a signed refusal, not a drop."""
    admission = _admit(bridge, trigger_id="forged-1", authenticated=False, refusal_reason=REFUSAL_UNAUTHENTICATED)

    assert admission.admitted is False
    assert admission.graph is None
    receipt = admission.receipt
    assert receipt.outcome == TRIGGER_OUTCOME_REFUSED
    assert receipt.refusal_reason == REFUSAL_UNAUTHENTICATED
    assert receipt.signature

    result = verify_receipt_document(
        receipt.to_dict(),
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
        body=_BODY,
    )
    assert result.ok, result.reason


def test_replayed_trigger_is_refused_and_the_refusal_is_a_chain_event(bridge: dict[str, object]) -> None:
    """A re-sent trigger id is refused; the refusal is itself an anchored receipt."""
    first = _admit(bridge, trigger_id="zap-42")
    assert first.admitted is True

    second = _admit(bridge, trigger_id="zap-42")
    assert second.admitted is False
    assert second.refusal_reason == REFUSAL_REPLAYED_TRIGGER
    assert second.receipt.outcome == TRIGGER_OUTCOME_REFUSED
    assert second.receipt.chain_entry_hash
    assert second.receipt.chain_entry_hash != first.receipt.chain_entry_hash

    result = verify_receipt_document(
        second.receipt.to_dict(),
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert result.ok, result.reason
    assert result.outcome == TRIGGER_OUTCOME_REFUSED


def test_replay_does_not_mint_a_second_admission(bridge: dict[str, object]) -> None:
    """The replayed trigger never projects a graph, so no second run is fired."""
    _admit(bridge, trigger_id="wk-7")
    replay = _admit(bridge, trigger_id="wk-7")
    assert replay.graph is None


def test_receipt_roundtrips_through_json(bridge: dict[str, object]) -> None:
    """A receipt stored by the platform as JSON reloads byte-identically."""
    receipt = _admit(bridge).receipt
    stored = json.dumps(receipt.to_dict(), sort_keys=True)
    reloaded = TriggerReceipt.from_dict(json.loads(stored))
    assert reloaded == receipt
    assert json.dumps(reloaded.to_dict(), sort_keys=True) == stored


# ---------------------------------------------------------------------------
# Determinism: payload digest and canonical graph projection
# ---------------------------------------------------------------------------


def test_identical_payload_bytes_produce_an_identical_digest() -> None:
    """Admission identity is a pure function of the payload bytes."""
    assert compute_payload_digest(_BODY) == compute_payload_digest(bytes(_BODY))
    assert compute_payload_digest(_BODY) != compute_payload_digest(_BODY + b" ")


def test_identical_payload_projects_an_identical_graph(bridge: dict[str, object]) -> None:
    """Two operators firing the same payload project the same canonical graph."""
    intent = {"title": "Ship the patch", "description": "cut a release", "role": "dev"}
    left = project_task_graph(platform="n8n", intent=intent)
    right = project_task_graph(platform="n8n", intent=dict(reversed(list(intent.items()))))

    assert left.graph_digest == right.graph_digest
    assert left.to_dict() == right.to_dict()


def test_graph_projection_orders_multi_step_payloads_deterministically() -> None:
    """A multi-step payload projects stable node ids and dependency edges."""
    intent = {
        "title": "Release train",
        "steps": [
            {"title": "run tests", "role": "qa"},
            {"title": "cut tag", "role": "dev"},
        ],
    }
    first = project_task_graph(platform="workato", intent=intent)
    second = project_task_graph(platform="workato", intent=json.loads(json.dumps(intent)))

    assert first.graph_digest == second.graph_digest
    assert len(first.nodes) == 2
    assert first.nodes[1].depends_on == (first.nodes[0].node_id,)


def test_different_payloads_project_different_graphs() -> None:
    """The graph digest separates distinct admissions."""
    left = project_task_graph(platform="n8n", intent={"title": "a"})
    right = project_task_graph(platform="n8n", intent={"title": "b"})
    assert left.graph_digest != right.graph_digest


def test_receipt_binds_the_projected_graph(bridge: dict[str, object]) -> None:
    """The admitted receipt carries the graph digest the payload projects."""
    admission = _admit(bridge)
    assert admission.graph is not None
    assert admission.receipt.graph_digest == admission.graph.graph_digest


def test_admitted_receipt_still_verifies_after_the_segment_is_archived(bridge: dict[str, object]) -> None:
    """Re-verification long after admission survives the retention boundary.

    The anchoring row moves from the live ``<date>.jsonl`` into a compressed
    ``archive/<date>.jsonl.gz`` segment during routine retention. Offline
    verification must still find it; reading only the live segment reports an
    honest receipt as unanchored, which is a false tamper verdict on exactly the
    long-after-the-fact check the receipt exists for.
    """
    receipt = _admit(bridge).receipt
    _archive_chain(bridge)

    result = verify_receipt_document(
        receipt.to_dict(),
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert result.ok, result.reason
    assert result.kind == "trigger"
    assert result.outcome == TRIGGER_OUTCOME_ADMITTED


# ---------------------------------------------------------------------------
# Outbound: chain-anchored status proofs
# ---------------------------------------------------------------------------


_EVENT_PAYLOAD = {
    "event_id": "evt-1",
    "kind": "post_task",
    "title": "Task t-42 failed",
    "body": "worker exited non-zero",
    "severity": "error",
    "task_id": "t-42",
    "session_id": None,
    "run_id": "run-9",
    "timestamp": 1_700_000_500.0,
    "labels": {},
    "details": {},
}


def _emit(bridge: dict[str, object], *, status: str = "failed", payload: dict | None = None):
    return emit_status_proof(
        root=bridge["root"],
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
        payload=payload if payload is not None else _EVENT_PAYLOAD,
        status=status,
        timestamp=1_700_000_500,
    )


def test_status_proof_verifies_against_the_chain_anchor(bridge: dict[str, object]) -> None:
    """A delivered callback re-verifies offline against the local chain."""
    proof = _emit(bridge)
    envelope = wrap_status_payload(_EVENT_PAYLOAD, proof)

    result = verify_receipt_document(
        envelope,
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert result.ok, result.reason
    assert result.kind == "status"
    assert result.chain_status == "failed"


def test_status_proof_still_verifies_after_the_segment_is_archived(bridge: dict[str, object]) -> None:
    """A delivered callback re-verifies after its anchor rolls into the archive.

    Same retention boundary as the trigger path: the ``status.proof.emitted``
    row is compressed into ``archive/<date>.jsonl.gz`` and the verifier must
    still resolve it rather than fall through to an unanchored (false tamper)
    verdict.
    """
    proof = _emit(bridge)
    envelope = wrap_status_payload(_EVENT_PAYLOAD, proof)
    _archive_chain(bridge)

    result = verify_receipt_document(
        envelope,
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert result.ok, result.reason
    assert result.kind == "status"
    assert result.chain_status == "failed"


def test_reordered_carried_payload_still_verifies(bridge: dict[str, object]) -> None:
    """A callback whose carried payload keys are reordered in transit still verifies.

    The producing-event digest canonicalises the payload with sorted keys, so a
    proxy that reserialises the JSON in a different key order is not mistaken for
    a tampered body. Only a changed *value* breaks the digest. Drop the sorted
    canonicalisation and this honest reserialisation reads as tampering.
    """
    proof = _emit(bridge)
    reordered = dict(reversed(list(_EVENT_PAYLOAD.items())))
    assert list(reordered) != list(_EVENT_PAYLOAD), "reordering must actually change key order"
    envelope = wrap_status_payload(reordered, proof)

    result = verify_receipt_document(
        envelope,
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert result.ok, result.reason
    assert result.chain_status == "failed"


def test_chain_recorded_status_mismatch_is_rejected(bridge: dict[str, object]) -> None:
    """A validly signed proof whose status disagrees with the chain row fails.

    The signature covers the status, and the anchor's ``proof_digest`` matches
    the binding, so neither the signature check nor the digest check catches a
    chain row whose ``status`` field was altered on its own. The dedicated
    ``status != chain`` gate is what rejects it and surfaces the recorded value.
    The anchor hash is outside the signed binding, so re-pointing the proof at
    the altered row keeps the signature valid.
    """
    from dataclasses import replace

    from bernstein.core.security.audit_chain import AuditChainStore, record_status_proof

    proof = _emit(bridge, status="failed")
    chain = AuditChainStore(bridge["audit_dir"], key=bridge["hmac_key"])
    altered = record_status_proof(
        chain=chain,
        event_id=proof.event_id,
        run_id=proof.run_id,
        status="succeeded",
        producing_event_digest=proof.producing_event_digest,
        proof_digest=proof.binding_digest(),
    )
    swapped: StatusProof = replace(proof, chain_entry_hash=altered.hmac)
    envelope = wrap_status_payload(_EVENT_PAYLOAD, swapped)

    result = verify_receipt_document(
        envelope,
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert not result.ok
    assert "status" in result.reason
    assert result.chain_status == "succeeded"


def test_status_flipped_in_transit_fails_and_reports_the_chain_status(bridge: dict[str, object]) -> None:
    """Flipping the reported status fails verify and surfaces the recorded one."""
    proof = _emit(bridge, status="failed")
    envelope = wrap_status_payload(_EVENT_PAYLOAD, proof)

    envelope[PROOF_ENVELOPE_KEY]["status"] = "succeeded"
    result = verify_receipt_document(
        envelope,
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert not result.ok
    assert result.chain_status == "failed"


def test_status_body_altered_in_transit_fails_verification(bridge: dict[str, object]) -> None:
    """Rewriting the carried event payload breaks the producing-event digest."""
    proof = _emit(bridge)
    envelope = wrap_status_payload(_EVENT_PAYLOAD, proof)
    envelope["severity"] = "info"

    result = verify_receipt_document(
        envelope,
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert not result.ok
    assert "producing event digest" in result.reason


def test_retried_callbacks_carry_byte_identical_envelopes(bridge: dict[str, object]) -> None:
    """A re-sent callback after a transient failure repeats the same bytes."""
    first = wrap_status_payload(_EVENT_PAYLOAD, _emit(bridge))
    second = wrap_status_payload(_EVENT_PAYLOAD, _emit(bridge))

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_proof_envelope_is_additive(bridge: dict[str, object]) -> None:
    """Existing consumers keep parsing: every original key survives unchanged."""
    envelope = wrap_status_payload(_EVENT_PAYLOAD, _emit(bridge))

    for key, value in _EVENT_PAYLOAD.items():
        assert envelope[key] == value
    assert set(envelope) == set(_EVENT_PAYLOAD) | {PROOF_ENVELOPE_KEY}


def test_unanchored_document_is_reported_not_silently_accepted(bridge: dict[str, object]) -> None:
    """A receipt whose anchor is absent from the chain fails verification."""
    receipt = _admit(bridge).receipt
    forged = receipt.to_dict()
    forged["chain_entry_hash"] = "0" * 64

    result = verify_receipt_document(
        forged,
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert not result.ok
    assert "anchor" in result.reason


def test_unrecognised_document_is_refused(bridge: dict[str, object]) -> None:
    """A document that is neither a trigger receipt nor a status proof fails."""
    result = verify_receipt_document(
        {"hello": "world"},
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
    )
    assert not result.ok
    assert result.kind == "unknown"


# ---------------------------------------------------------------------------
# Refusal budget: the unauthenticated path cannot grow the chain without bound
# ---------------------------------------------------------------------------


def _refuse(bridge: dict[str, object], budget: RefusalBudget, *, trigger_id: str):
    return admit_trigger(
        root=bridge["root"],
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
        platform="n8n",
        request_path="/webhook",
        trigger_id=trigger_id,
        body=_BODY,
        scope="task:create",
        timestamp=1_700_000_000,
        authenticated=False,
        refusal_reason=REFUSAL_UNAUTHENTICATED,
        budget=budget,
    )


def test_unauthenticated_refusals_are_anchored_up_to_the_budget(bridge: dict[str, object]) -> None:
    """Refusals inside the budget each get their own signed receipt."""
    budget = RefusalBudget(root=bridge["root"], limit=3, window_s=60)

    for index in range(3):
        admission = _refuse(bridge, budget, trigger_id=f"junk-{index}")
        assert admission.receipt is not None
        assert admission.admitted is False


def test_refusals_past_the_budget_are_still_refused_without_a_receipt(bridge: dict[str, object]) -> None:
    """Anonymous flooding cannot force an unbounded signed append per request."""
    budget = RefusalBudget(root=bridge["root"], limit=2, window_s=60)
    for index in range(2):
        _refuse(bridge, budget, trigger_id=f"junk-{index}")

    flooded = _refuse(bridge, budget, trigger_id="junk-over")
    assert flooded.admitted is False
    assert flooded.receipt is None
    # The trigger is still refused; only the per-request receipt is withheld.
    assert flooded.refusal_reason == REFUSAL_UNAUTHENTICATED


def test_the_chain_records_that_refusals_were_suppressed(bridge: dict[str, object]) -> None:
    """The next window's first anchored refusal carries the suppressed count."""
    from bernstein.core.security.audit_chain import (
        EVENT_TRIGGER_RECEIPT_REFUSED,
        AuditChainStore,
    )

    budget = RefusalBudget(root=bridge["root"], limit=1, window_s=60)
    _refuse(bridge, budget, trigger_id="first")
    for index in range(4):
        _refuse(bridge, budget, trigger_id=f"flood-{index}")

    # A later window: the first anchored refusal reports the suppressed run.
    later = RefusalBudget(root=bridge["root"], limit=1, window_s=0)
    _refuse(bridge, later, trigger_id="next-window")

    chain = AuditChainStore(bridge["audit_dir"], key=bridge["hmac_key"])
    counts = [
        event.details.get("suppressed_refusals")
        for event in chain.query(event_type=EVENT_TRIGGER_RECEIPT_REFUSED)
        if event.details.get("suppressed_refusals")
    ]
    assert counts == [4]


def test_replay_refusals_are_not_budgeted(bridge: dict[str, object]) -> None:
    """Producing a replay refusal needs a valid signature, so it is never capped."""
    budget = RefusalBudget(root=bridge["root"], limit=0, window_s=60)
    _admit(bridge, trigger_id="authentic")

    replay = admit_trigger(
        root=bridge["root"],
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
        platform="n8n",
        request_path="/webhook",
        trigger_id="authentic",
        body=_BODY,
        scope="task:create",
        timestamp=1_700_000_000,
        authenticated=True,
        budget=budget,
    )
    assert replay.admitted is False
    assert replay.receipt is not None
    assert replay.receipt.refusal_reason == REFUSAL_REPLAYED_TRIGGER


def test_admissions_are_not_budgeted(bridge: dict[str, object]) -> None:
    """A valid trigger is never withheld a receipt by the refusal budget."""
    budget = RefusalBudget(root=bridge["root"], limit=0, window_s=60)
    admission = admit_trigger(
        root=bridge["root"],
        audit_dir=bridge["audit_dir"],
        hmac_key=bridge["hmac_key"],
        platform="n8n",
        request_path="/webhook",
        trigger_id="fine",
        body=_BODY,
        scope="task:create",
        timestamp=1_700_000_000,
        budget=budget,
    )
    assert admission.admitted is True
    assert admission.receipt is not None
