"""Authority questions answered only from the exported bundle (#5059)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conformance.auditor import recorder

if TYPE_CHECKING:
    from tests.conformance.auditor.bundle import BundleReader


@pytest.mark.question(3)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "sub-agent spawn has no parent_identity_id or signed authorization grant "
        "binding the delegation to an authorizing principal (#5046)"
    ),
)
def test_q3_was_the_subagent_authorized_to_act_and_by_whom(bundle_reader: BundleReader) -> None:
    """Q3: was the sub-agent authorized to act, and by whom?

    The bundle must prove the sub-agent was spawned under an explicit, signed
    authorization grant from an identified principal, rather than an unauthenticated
    local parent_agent_id string.
    """
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    events = audit["events"]
    delegations = [event for event in events if event["event_type"] == "agent.delegated"]
    assert delegations, "the bundle must record the sub-agent delegation event"

    for delegation in delegations:
        details = delegation.get("details", {})
        # Must carry a cryptographic parent identity id and authorization grant ref
        parent_identity = details.get("parent_identity_id")
        grant_id = details.get("grant_id")
        assert parent_identity, "delegation does not name parent_identity_id (#5046)"
        assert grant_id, "delegation does not reference an authorization grant (#5046)"


@pytest.mark.question(4)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "the exported bundle carries no delegation grant record specifying permitted scope (#5047)"
    ),
)
def test_q4_what_exactly_was_the_subagent_permitted_to_do(bundle_reader: BundleReader) -> None:
    """Q4: what exactly was it permitted to do?

    The bundle must export a grant record stating the sub-agent's permitted scope
    (tools, file paths, endpoints, and delegation depth).
    """
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    grants = [
        event
        for event in audit["events"]
        if event["event_type"] in ("authority.grant", "delegation.grant")
    ]
    assert grants, "the bundle must contain an authority grant record (#5047)"

    grant = grants[0]
    scope = grant.get("details", {}).get("scope", {})
    assert "allowed_tools" in scope, "grant must declare allowed_tools (#5047)"
    assert "allowed_files" in scope, "grant must declare allowed_files (#5047)"


@pytest.mark.question(5)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="no grant boundary exists to compare sub-agent actions against (#5047)",
)
def test_q5_did_the_subagent_stay_inside_that_permission(bundle_reader: BundleReader) -> None:
    """Q5: did it stay inside that permission?

    Without an exported grant record declaring scope bounds, sub-agent actions
    cannot be proven contained within delegated authority.
    """
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    events = audit["events"]

    grants = {
        event["resource_id"]: event
        for event in events
        if event["event_type"] in ("authority.grant", "delegation.grant")
    }
    assert grants, "no delegation grant found in audit events to evaluate boundary against (#5047)"

    # Check that each sub-agent action was validated against the grant
    subagent_actions = [
        event
        for event in events
        if event.get("actor") == "agent-b" and event["event_type"] != "agent.delegated"
    ]
    assert subagent_actions, "sub-agent actions must exist in the bundle"
    for action in subagent_actions:
        assert action.get("details", {}).get("grant_id") in grants, (
            "action does not bind to an active delegation grant (#5047)"
        )


@pytest.mark.question(21)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "the exported bundle has no delegation graph linking principals derived from the same grant (#5055)"
    ),
)
def test_q21_which_other_principals_hold_authority_derived_from_the_same_grant(
    bundle_reader: BundleReader,
) -> None:
    """Q21: which other principals hold authority derived from the same grant?

    The bundle must carry a verifiable authority graph linking all principals
    whose authority stems from the root grant.
    """
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    authority_graph = audit.get("authority_graph") or audit.get("delegation_tree")
    assert authority_graph is not None, (
        "bundle must export an authority graph or delegation tree (#5055)"
    )
    assert isinstance(authority_graph, dict) and "nodes" in authority_graph, (
        "authority graph must link principal nodes and delegation edges (#5055)"
    )
