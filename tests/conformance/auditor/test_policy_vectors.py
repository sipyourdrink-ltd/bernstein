"""Policy and approval questions answered only from the exported bundle (#5060)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conformance.auditor import recorder

if TYPE_CHECKING:
    from tests.conformance.auditor.bundle import BundleReader


@pytest.mark.question(6)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "the exported bundle has no policy reference or version binding the decision "
        "that allowed the tool call; the bundle exports no GovernanceDecision record "
        "(core/security/governance.py:217) — that is the field a follow-up must add (#5060)"
    ),
)
def test_q6_the_policy_and_version_allowing_the_tool_call_is_recorded(bundle_reader: BundleReader) -> None:
    """Q6: which policy, at which version, allowed the tool call.

    When ``GovernanceDecision`` reaches the bundle (``core/security/governance.py:217``),
    each ``tool_called`` journal event should carry ``policy_id`` and ``policy_version``
    binding the allow decision to the policy that made it.
    """
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    events = run["journal"]["events"]
    tool_calls = [e for e in events if e["event"] == "tool_called"]
    assert tool_calls, "no tool_called event in journal"
    for call in tool_calls:
        assert call.get("policy_id"), "decision does not name the policy (#5060)"
        assert call.get("policy_version"), "decision does not name the policy version (#5060)"


@pytest.mark.question(11)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=("the exported bundle does not record whether human approval was required for the action (#5060)"),
)
def test_q11_the_record_states_whether_human_approval_was_required(bundle_reader: BundleReader) -> None:
    """Q11: was human approval required for the final action."""
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    events = audit["events"]
    # Real approval events emitted by approval_gate.py:488,521 are approval_pending
    changes = [e for e in events if e["event_type"] == "repo.changed"]
    assert changes, "no repo.changed event in audit receipt"
    for change in changes:
        assert "approval_required" in change["details"], "repo.changed does not record approval_required (#5060)"


@pytest.mark.question(12)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "the exported bundle does not record the approver identity or approval "
        "timestamp in approval_resolved events; approval_gate.py:571-578 now records "
        "outcome and decision_source for timeout-defaults but approved_by and timestamp "
        "are absent for human approvals (#5060)"
    ),
)
def test_q12_the_approver_and_approval_timestamp_are_recorded(bundle_reader: BundleReader) -> None:
    """Q12: if approval was required, who approved it and when.

    The real approval event names emitted by the approval gate are
    ``approval_pending`` (core/orchestration/approval_gate.py:488,521) and
    ``approval_resolved`` (core/orchestration/approval_gate.py:504,543,571).
    This test matches those exact names so it will turn XPASS as soon as the
    approver identity and timestamp reach the bundle.
    """
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    events = audit["events"]
    # Match the real event names emitted by approval_gate.py, not a prefix guess
    approvals = [
        e for e in events if e["event_type"] in ("approval_pending", "approval_resolved")
    ]
    assert approvals, "no approval event recorded in audit receipt (#5060)"
    for approval in approvals:
        assert approval["details"].get("approved_by"), "approval record missing approved_by (#5060)"
        assert approval.get("timestamp"), "approval record missing timestamp (#5060)"


@pytest.mark.question(13)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "the exported bundle has no policy hash or version in force at decision time; "
        "the bundle exports no GovernanceDecision record "
        "(core/security/governance.py:217) — that is the field a follow-up must add (#5060)"
    ),
)
def test_q13_the_policy_version_in_force_at_decision_time_is_recorded(bundle_reader: BundleReader) -> None:
    """Q13: which policy version was in force at decision time, not export time.

    When ``GovernanceDecision`` reaches the bundle (``core/security/governance.py:217``),
    events should carry ``policy_digest`` capturing the hash of the policy that was
    evaluated at the moment of decision.
    """
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    events = run["journal"]["events"]
    assert events, "no events in journal"
    for event in events:
        if event.get("event") in ("tool_called", "artifact_written"):
            assert event.get("policy_digest"), "event has no policy_digest anchored at decision time (#5060)"
