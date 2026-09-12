"""Conformance vectors for the four authority questions (#5059).

Q3  Was the sub-agent authorized, and by whom.
Q4  What exactly was it permitted to do.
Q5  Did it stay inside that.
Q21 Who else holds authority from the same grant.

All four are ``xfail(strict=True)``: the audit receipt records that a
delegation happened (``agent.delegated``) but carries no authorization
grant, no permitted-action scope, and no grant-propagation record.  Until
those fields land, the scoreboard reports them as unanswered rather than
asserting something weaker than the question actually asks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conformance.auditor import recorder

if TYPE_CHECKING:
    from tests.conformance.auditor.bundle import BundleReader

# ---------------------------------------------------------------------------
# Fields the authority questions need but the bundle does not yet carry
# ---------------------------------------------------------------------------

#: Fields any of which would identify the authorization grant.
_GRANT_FIELDS = ("grant_id", "authorization_id", "granted_by", "delegation_grant")

#: Fields any of which would describe the permitted action scope.
_SCOPE_FIELDS = ("permitted_actions", "permission_scope", "allowed_operations", "capability_set")

#: Fields any of which would record a boundary-compliance check.
_COMPLIANCE_FIELDS = ("stayed_within_scope", "boundary_check", "scope_violation", "compliance_status")

#: Fields any of which would list co-grantees from the same delegation.
_PROPAGATION_FIELDS = ("co_grantees", "grant_propagation", "sibling_agents", "grant_chain")


# ---------------------------------------------------------------------------
# Q3 -- was the sub-agent authorized, and by whom
# ---------------------------------------------------------------------------


@pytest.mark.question(3)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "the audit receipt records an agent.delegated event naming the "
        "delegating and delegated agent, but carries no grant identifier "
        "and no record of who authorized the delegation; an auditor cannot "
        "determine whether the sub-agent was authorized, let alone by whom, "
        "without a grant_id or equivalent on the delegation event (#5059)"
    ),
)
def test_q3_delegation_event_records_authorization_grant(bundle_reader: BundleReader) -> None:
    """Q3: was the sub-agent authorized, and by whom?

    The ``agent.delegated`` audit event records that agent-a delegated to
    agent-b but carries no authorization grant identifier and no grantor
    identity beyond the delegating agent itself.  An auditor cannot
    determine whether the delegation was pre-authorized and by whom
    without a ``grant_id`` or equivalent.
    """
    receipt = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    delegations = [e for e in receipt["events"] if e.get("event_type") == "agent.delegated"]
    assert delegations, "no agent.delegated event found in audit receipt"

    for event in delegations:
        details = {**event, **(event.get("details") or {})}
        found = [f for f in _GRANT_FIELDS if f in details]
        assert found, (
            f"agent.delegated event at seq {event.get('seq')} carries no authorization grant field; "
            f"looked for {list(_GRANT_FIELDS)}"
        )


# ---------------------------------------------------------------------------
# Q4 -- what exactly was it permitted to do
# ---------------------------------------------------------------------------


@pytest.mark.question(4)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "no event in the audit receipt carries a permitted-action scope: "
        "delegation is recorded but what the delegated agent was allowed to "
        "do -- which tools, which resources, which endpoints -- is never "
        "written to any bundle file, so the scope cannot be read off the "
        "evidence (#5059)"
    ),
)
def test_q4_bundle_records_permitted_action_scope(bundle_reader: BundleReader) -> None:
    """Q4: what exactly was the sub-agent permitted to do?

    An auditor needs to read the permission scope granted to the delegated
    agent from the bundle.  The audit receipt records tool calls, file
    reads, and model requests but never states which subset of those was
    explicitly permitted, so the scope cannot be determined from the
    evidence alone.
    """
    receipt = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)

    all_details: list[dict[str, object]] = [
        {**event, **(event.get("details") or {})} for event in receipt["events"]
    ]
    found_scope = [
        event
        for event in all_details
        if any(f in event for f in _SCOPE_FIELDS)
    ]
    assert found_scope, (
        f"no event in the audit receipt carries a permission scope field; "
        f"looked for {list(_SCOPE_FIELDS)} across {len(receipt['events'])} events"
    )


# ---------------------------------------------------------------------------
# Q5 -- did it stay inside that
# ---------------------------------------------------------------------------


@pytest.mark.question(5)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "without a recorded permission scope (Q4) there is nothing to compare "
        "the observed actions against; the audit receipt records what the agent "
        "did but not whether each action was inside an authorized boundary; no "
        "boundary-compliance check or scope-violation field appears in any "
        "bundle file (#5059)"
    ),
)
def test_q5_bundle_records_scope_compliance(bundle_reader: BundleReader) -> None:
    """Q5: did the sub-agent stay inside its permitted scope?

    Answering this question requires both the permitted scope (Q4) and a
    record of whether each action fell within it.  The bundle carries
    neither: there is no scope to compare against and no per-action
    compliance attestation.
    """
    receipt = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)

    all_details: list[dict[str, object]] = [
        {**event, **(event.get("details") or {})} for event in receipt["events"]
    ]
    found_compliance = [
        event
        for event in all_details
        if any(f in event for f in _COMPLIANCE_FIELDS)
    ]
    assert found_compliance, (
        f"no event in the audit receipt carries a scope-compliance field; "
        f"looked for {list(_COMPLIANCE_FIELDS)} across {len(receipt['events'])} events"
    )


# ---------------------------------------------------------------------------
# Q21 -- who else holds authority from the same grant
# ---------------------------------------------------------------------------


@pytest.mark.question(21)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "the bundle carries no grant-propagation record: there is no list of "
        "co-grantees, no grant chain linking sibling agents to the same "
        "authorization, and no field naming other principals that inherited "
        "authority from the same delegation; an auditor cannot determine who "
        "else acted under the same grant without this information (#5059)"
    ),
)
def test_q21_bundle_records_grant_propagation(bundle_reader: BundleReader) -> None:
    """Q21: who else holds authority from the same grant?

    If the delegation that authorized the sub-agent was itself derived from
    a broader grant, an auditor needs to know who else can act under that
    grant.  The bundle contains no grant-chain or co-grantee record of any
    kind.
    """
    receipt = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)

    all_details: list[dict[str, object]] = [
        {**event, **(event.get("details") or {})} for event in receipt["events"]
    ]
    all_details.append(receipt)

    found_propagation = [
        item
        for item in all_details
        if any(f in item for f in _PROPAGATION_FIELDS)
    ]
    assert found_propagation, (
        f"no event or top-level field in the audit receipt carries a "
        f"grant-propagation record; looked for {list(_PROPAGATION_FIELDS)}"
    )
