"""Policy and human sign-off questions 6, 11, 12 and 13 (#5060).

Only exported audit events are read. Missing decision-time policy references
and approval evidence remain strict expected failures, not invented evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
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
