"""Policy and human sign-off questions 6, 11, 12 and 13 (#5060).

Only exported bundle receipts are read. Missing decision-time policy references
and approval evidence remain strict expected failures, not invented evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import pytest

from bernstein.core.replay.run_receipt import verify_run_receipt
from tests.integration.conformance.auditor.bundle_reader import BundleReader


def _action(auditor_bundle: BundleReader, event_type: str) -> dict[str, Any]:
    receipt = auditor_bundle.read_json("audit-receipt.json")
    events = [event for event in receipt["events"] if event.get("event_type") == event_type]
    assert len(events) == 1, f"expected one recorded {event_type} action"
    return events[0]


def _tool_decision(auditor_bundle: BundleReader) -> dict[str, Any]:
    tool_call = _action(auditor_bundle, "tool.invoked")
    decision = tool_call.get("details", {}).get("governance_decision")
    assert isinstance(decision, dict), (
        "tool.invoked has no exported GovernanceDecision binding; missing "
        "policy_id and policy_version cannot be recovered from inputs_hash"
    )
    assert decision.get("verdict") == "allow", "the decision did not allow this tool call"
    return decision


def _verified_run_receipt(auditor_bundle: BundleReader) -> dict[str, Any]:
    receipt = auditor_bundle.read_json("run-receipt.json")
    assert isinstance(receipt.get("audit_range"), dict), "missing exported signed tool event audit range"
    tool_events = [
        event for event in receipt.get("audit_range", {}).get("events", []) if event.get("event_type") == "tool.invoked"
    ]
    assert len(tool_events) == 1, "missing unique exported signed tool event"
    # Only exported bytes are evidence. This pure verifier checks integrity
    # without a workspace or HMAC key; operator-key trust remains separate.
    integrity = verify_run_receipt(json.dumps(receipt).encode("utf-8"))
    assert integrity.ok, f"exported run receipt integrity failed: {integrity.errors}"
    assert tool_events[0] == _action(auditor_bundle, "tool.invoked"), (
        "tool decision attachment differs from the signed tool event"
    )
    return receipt


def _final_approval(auditor_bundle: BundleReader) -> dict[str, Any]:
    final_action = _action(auditor_bundle, "repo.changed")
    approval = final_action.get("details", {}).get("approval")
    assert isinstance(approval, dict), (
        "repo.changed has no approval record: missing required and outcome; "
        "absence does not mean approval was unnecessary or granted"
    )
    assert type(approval.get("required")) is bool, "approval.required must be explicit"
    if approval["required"]:
        assert approval.get("outcome") in {"granted", "rejected", "expired", "pending"}, (
            "required approval must distinguish a grant from expiry (#5051)"
        )
    else:
        assert approval.get("outcome") == "not_required"
    return approval


@pytest.mark.auditor_question(6)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#5060: exported tool action lacks a GovernanceDecision binding with "
        "policy_id and policy_version; inputs_hash is not a policy reference"
    ),
)
def test_which_policy_at_which_version_allowed_the_tool_call(auditor_bundle: BundleReader) -> None:
    decision = _tool_decision(auditor_bundle)
    assert decision.get("policy_id"), "GovernanceDecision is missing policy_id"
    assert decision.get("policy_version"), "GovernanceDecision is missing policy_version"


@pytest.mark.auditor_question(11)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#5060: final action lacks approval.required and approval.outcome; "
        "no approval needed, a human grant and wait expiry (#5051) are indistinguishable"
    ),
)
def test_was_human_approval_required_for_the_final_action(auditor_bundle: BundleReader) -> None:
    _final_approval(auditor_bundle)


@pytest.mark.auditor_question(12)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#5060: final action lacks approval.required, outcome, approved_by and "
        "approved_at; ApprovalResult names no approver and expiry is not sign-off (#5051)"
    ),
)
def test_if_required_who_approved_the_final_action_and_when(auditor_bundle: BundleReader) -> None:
    approval = _final_approval(auditor_bundle)
    if approval["required"] and approval["outcome"] == "granted":
        approver = approval.get("approved_by")
        assert isinstance(approver, str) and approver.strip(), "missing human approver approved_by"
        approved_at = approval.get("approved_at")
        assert isinstance(approved_at, str) and approved_at, "missing sign-off timestamp approved_at"
        try:
            timestamp = datetime.fromisoformat(approved_at)
        except ValueError:
            pytest.fail("approved_at is not an ISO timestamp")
        assert timestamp.tzinfo is not None, "approved_at must identify an unambiguous instant"
    else:
        assert not approval.get("approved_by"), "no grant occurred; do not attribute human sign-off"
        assert not approval.get("approved_at"), "expiry or no approval needed is not a sign-off time"


@pytest.mark.auditor_question(13)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#5060: GovernanceDecision lacks decision-bound policy_id and policy_version; "
        "a schema version or export-time policy cannot identify the policy in force"
    ),
)
def test_which_policy_version_was_in_force_at_decision_time_not_export_time(
    auditor_bundle: BundleReader,
) -> None:
    decision = _tool_decision(auditor_bundle)
    assert decision.get("timestamp") is not None, "missing decision timestamp"
    assert decision.get("policy_id"), "missing decision-time policy_id"
    assert decision.get("policy_version"), "missing decision-time policy_version, not schema v"
    assert decision.get("journal_entry_hash"), "the policy reference must be anchored with the decision"

    run_receipt = _verified_run_receipt(auditor_bundle)
    anchors = [
        entry for entry in run_receipt["spine"]["entries"] if entry["entry_hash"] == decision["journal_entry_hash"]
    ]
    assert len(anchors) == 1, "no unique exported spine entry anchors this decision"
    anchor = anchors[0]
    assert anchor["actor"] == "bernstein.governance", "the anchor is not a governance decision"
    assert decision.get("run_id") == run_receipt["run_id"], "the decision belongs to another run"
    assert anchor["timestamp"] == decision["timestamp"], "the anchor has a different decision time"

    # GovernanceDecision's current canonical binding includes context, not
    # arbitrary top-level labels. Restate its wire fields without reading the
    # producer: an export-time policy label must also occur in the hashed bytes.
    context = decision.get("context")
    assert isinstance(context, dict), "missing canonical decision context"
    for field in ("policy_id", "policy_version"):
        assert context.get(field) == decision[field], f"{field} is not in the decision's anchored context"
    fields = ("v", "run_id", "subject", "action", "verdict", "inputs_hash", "timestamp", "context")
    assert all(field in decision for field in fields), "missing canonical GovernanceDecision fields"
    binding = {field: decision[field] for field in fields}
    canonical = json.dumps(binding, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert anchor["content_hash"] == "sha256:" + hashlib.sha256(canonical).hexdigest(), (
        "exported policy/version or decision bytes do not match the decision-time anchor"
    )


@pytest.mark.parametrize("anchor_kind", ["arbitrary", "unrelated", "edited_anchor"])
def test_q13_rejects_policy_labels_without_a_decision_anchor(auditor_bundle: BundleReader, anchor_kind: str) -> None:
    """Negative controls edit a receipt in memory, never the scenario export."""
    receipt = auditor_bundle.read_json("audit-receipt.json")
    run_receipt = auditor_bundle.read_json("run-receipt.json")
    tool_call = next(event for event in receipt["events"] if event["event_type"] == "tool.invoked")
    unrelated = run_receipt["spine"]["entries"][0]
    tool_call["details"]["governance_decision"] = {
        "verdict": "allow",
        "timestamp": unrelated["timestamp"],
        "policy_id": "export-time-label",
        "policy_version": "export-time-version",
        "journal_entry_hash": "arbitrary-nonempty" if anchor_kind == "arbitrary" else unrelated["entry_hash"],
    }
    if anchor_kind == "edited_anchor":
        decision = tool_call["details"]["governance_decision"]
        decision.update(
            v=1,
            run_id=run_receipt["run_id"],
            subject=tool_call["actor"],
            action=tool_call["resource_id"],
            inputs_hash="sha256:" + "0" * 64,
            context={"policy_id": decision["policy_id"], "policy_version": decision["policy_version"]},
        )
        binding = {
            key: value
            for key, value in decision.items()
            if key not in {"journal_entry_hash", "policy_id", "policy_version"}
        }
        canonical = json.dumps(binding, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        # Deliberately invalid evidence: retain the old entry hash and signed
        # head while replacing the bytes that they supposedly anchor.
        unrelated["actor"] = "bernstein.governance"
        unrelated["content_hash"] = "sha256:" + hashlib.sha256(canonical).hexdigest()

    class EditedReceipt(BundleReader):
        def read_json(self, name: str) -> dict[str, Any]:
            return {"audit-receipt.json": receipt, "run-receipt.json": run_receipt}[name]

    message = "run receipt integrity failed" if anchor_kind == "edited_anchor" else "differs from the signed tool event"
    with pytest.raises(AssertionError, match=message):
        test_which_policy_version_was_in_force_at_decision_time_not_export_time(EditedReceipt(auditor_bundle.root))


@pytest.mark.parametrize("edit", ["audit_only_attachment", "missing_signed_event"])
def test_policy_binding_requires_the_exported_signed_tool_event(auditor_bundle: BundleReader, edit: str) -> None:
    receipt = auditor_bundle.read_json("audit-receipt.json")
    run_receipt = auditor_bundle.read_json("run-receipt.json")
    if edit == "audit_only_attachment":
        next(event for event in receipt["events"] if event["event_type"] == "tool.invoked")["details"][
            "governance_decision"
        ] = {"policy_id": "export-only"}
    else:
        run_receipt.pop("audit_range")

    class EditedReceipt(BundleReader):
        def read_json(self, name: str) -> dict[str, Any]:
            return {"audit-receipt.json": receipt, "run-receipt.json": run_receipt}[name]

    message = (
        "differs from the signed tool event"
        if edit == "audit_only_attachment"
        else "missing exported signed tool event"
    )
    with pytest.raises(AssertionError, match=message):
        _verified_run_receipt(EditedReceipt(auditor_bundle.root))


def test_exported_tool_action_matches_its_signed_run_receipt(auditor_bundle: BundleReader) -> None:
    """Positive control uses the real, unedited export, with no added evidence."""
    _verified_run_receipt(auditor_bundle)
