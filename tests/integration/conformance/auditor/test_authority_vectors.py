"""Authority conformance vectors (#5059, part of #5054): questions 3, 4, 5 and 21.

Only exported bundle receipts are read through :class:`BundleReader`. All four
vectors currently record strict ``xfail`` expectations because the exported
evidence logs that delegation occurred (``agent.delegated`` / ``agent_spawned``)
without a signed authorization grant, permitted-action scope, per-action
boundary-compliance check, or grant-propagation chain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.integration.conformance.auditor import scenario

if TYPE_CHECKING:
    from tests.integration.conformance.auditor.bundle_reader import BundleReader

_GRANT_FIELDS = ("grant_id", "authorization_id", "granted_by", "delegation_grant", "authorizer")
_SCOPE_FIELDS = (
    "permitted_actions",
    "permission_scope",
    "allowed_operations",
    "allowed_tools",
    "capability_set",
    "scope",
)
_COMPLIANCE_FIELDS = (
    "stayed_within_scope",
    "boundary_check",
    "scope_violation",
    "compliance_status",
    "within_scope",
)
_PROPAGATION_FIELDS = (
    "co_grantees",
    "grant_propagation",
    "sibling_agents",
    "grant_chain",
    "derived_principals",
)


def _all_exported_events(auditor_bundle: BundleReader) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read audit-receipt and run-receipt events from the exported bundle only."""
    audit_receipt = auditor_bundle.read_json(scenario.AUDIT_RECEIPT_NAME)
    run_receipt = auditor_bundle.read_json(scenario.RUN_RECEIPT_NAME)
    audit_events: list[dict[str, Any]] = list(audit_receipt.get("events", []))
    journal_events: list[dict[str, Any]] = list(run_receipt.get("journal", {}).get("events", []))
    return audit_events, journal_events


def _merged_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten top-level event fields and nested ``details`` into inspectable maps."""
    records: list[dict[str, Any]] = []
    for event in events:
        details = event.get("details")
        merged = {**event, **(details if isinstance(details, dict) else {})}
        records.append(merged)
    return records


@pytest.mark.auditor_question(3)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#5059 / #5046: agent.delegated records actor/parent agent-A delegating to agent-B, "
        "but neither receipt carries a grant_id, authorization_id or authorizer grant record "
        "proving the sub-agent was authorized and by whom"
    ),
)
def test_question3_subagent_authorization_and_authorizer(auditor_bundle: BundleReader) -> None:
    """Q3: was the sub-agent authorized to act, and by whom?"""
    audit_events, _ = _all_exported_events(auditor_bundle)
    delegations = [
        record
        for record in _merged_records(audit_events)
        if record.get("event_type") == "agent.delegated" and record.get("resource_id") == scenario.AGENT_B
    ]
    assert delegations, "expected an agent.delegated audit event for sub-agent B"

    for record in delegations:
        found = [field for field in _GRANT_FIELDS if record.get(field)]
        assert found, (
            f"agent.delegated carries no authorization grant or authorizer field; "
            f"looked for {list(_GRANT_FIELDS)}, got keys {sorted(record.keys())}"
        )


@pytest.mark.auditor_question(4)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#5059 / #5046: neither run-receipt.json nor audit-receipt.json records a "
        "permitted_actions / permission_scope / capability_set grant bounding what "
        "sub-agent B was permitted to do"
    ),
)
def test_question4_subagent_permitted_scope(auditor_bundle: BundleReader) -> None:
    """Q4: what exactly was the sub-agent permitted to do?"""
    audit_events, journal_events = _all_exported_events(auditor_bundle)
    records = _merged_records(audit_events) + _merged_records(journal_events)
    found_scope = [record for record in records if any(record.get(field) for field in _SCOPE_FIELDS)]
    assert found_scope, (
        f"neither receipt declares the sub-agent's permitted action scope; "
        f"looked for {list(_SCOPE_FIELDS)} across {len(records)} exported events"
    )


@pytest.mark.auditor_question(5)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#5059 / #5046 / #5055: without a recorded permission scope (Q4) and a per-action "
        "boundary-compliance field (stayed_within_scope / boundary_check / compliance_status), "
        "the bundle cannot prove the sub-agent stayed inside its grant"
    ),
)
def test_question5_subagent_stayed_within_permission(auditor_bundle: BundleReader) -> None:
    """Q5: did the sub-agent stay inside that permission?"""
    audit_events, journal_events = _all_exported_events(auditor_bundle)
    records = _merged_records(audit_events) + _merged_records(journal_events)
    found_compliance = [record for record in records if any(field in record for field in _COMPLIANCE_FIELDS)]
    assert found_compliance, (
        f"neither receipt records per-action boundary compliance against a grant; "
        f"looked for {list(_COMPLIANCE_FIELDS)} across {len(records)} exported events"
    )


@pytest.mark.auditor_question(21)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#5059 / #5047: the bundle carries no grant-propagation graph, grant_chain or "
        "co_grantees list showing which other principals hold authority from the same grant"
    ),
)
def test_question21_other_principals_holding_authority_from_same_grant(
    auditor_bundle: BundleReader,
) -> None:
    """Q21: which other principals hold authority derived from the same grant?"""
    audit_receipt = auditor_bundle.read_json(scenario.AUDIT_RECEIPT_NAME)
    run_receipt = auditor_bundle.read_json(scenario.RUN_RECEIPT_NAME)

    records = (
        _merged_records(list(audit_receipt.get("events", [])))
        + _merged_records(list(run_receipt.get("journal", {}).get("events", [])))
        + [audit_receipt, run_receipt]
    )
    found_propagation = [item for item in records if any(item.get(field) for field in _PROPAGATION_FIELDS)]
    assert found_propagation, (
        f"no exported receipt or event carries a grant-propagation or co-grantee record; "
        f"looked for {list(_PROPAGATION_FIELDS)}"
    )
