"""Attribution vectors (#5058).

Four vectors land here: questions 1, 2, 7 and 14. Every vector answers
its question from the exported bundle alone, or does not claim to have
answered it.

All four questions cannot be answered by today's evidence, and each is
an ``xfail(strict=True)`` naming the field that is missing and the issue
that would add it. Strict matters twice over: the vector never flatters
the score, and the day the field lands the build fails until the vector
is un-marked. No evidence field is added here to make one pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.integration.conformance.auditor import scenario

if TYPE_CHECKING:
    from tests.integration.conformance.auditor.bundle_reader import BundleReader


@pytest.mark.auditor_question(1)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "no receipt names the initiating principal: neither run-receipt.json "
        "nor audit-receipt.json carries a principal, initiator or started_by "
        "field at the run level, so the bundle cannot say who started the run "
        "(#5057 harness landed; attribution producer work not yet assigned)"
    ),
)
def test_question1_initiating_principal(auditor_bundle: BundleReader) -> None:
    """Q1: which principal initiated the run?

    The scenario's person who started agent A is recorded somewhere in
    the bundle. Today's evidence does not carry it: the audit chain has
    a ``run.started`` event naming the run and the first agent, but no
    field names the human who started it.
    """
    audit_receipt = auditor_bundle.read_json(scenario.AUDIT_RECEIPT_NAME)
    run_receipt = auditor_bundle.read_json(scenario.RUN_RECEIPT_NAME)

    # The run-level principal field would live in one of these two receipts.
    run_started_event = next(
        (event for event in audit_receipt["events"] if event.get("event_type") == "run.started"),
        None,
    )
    assert run_started_event is not None, "the audit chain has no run.started event"

    # The field that would answer this does not exist today.
    PRINCIPAL_FIELDS = ["principal", "initiator", "started_by", "initiated_by"]
    principal_at_run_level = (
        run_receipt.get("principal")
        or run_receipt.get("initiator")
        or any(run_started_event.get(field) for field in PRINCIPAL_FIELDS)
    )

    assert principal_at_run_level, (
        f"neither receipt names the initiating principal; "
        f"run_receipt keys: {list(run_receipt.keys())}, "
        f"run.started event keys: {list(run_started_event.keys())}"
    )
    # When the field exists, assert it matches the scenario constant.
    # Today this line is never reached.
    assert principal_at_run_level == scenario.PRINCIPAL


@pytest.mark.auditor_question(2)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "audit events carry no agent attribution: the HMAC chain records "
        "an actor (the principal or agent), but tool calls, reads and model "
        "calls do not name which agent performed them, so question 2 cannot "
        "be answered per action (#5057 harness landed; attribution producer "
        "work not yet assigned)"
    ),
)
def test_question2_agent_per_action(auditor_bundle: BundleReader) -> None:
    """Q2: which agent performed each recorded action?

    The scenario's tool call was performed by agent B, not A. Today's
    evidence does not carry per-action agent attribution: the audit chain
    names an actor, which is sometimes a principal and sometimes an agent,
    but tool calls and model calls do not name the agent that made them.
    """
    audit_receipt = auditor_bundle.read_json(scenario.AUDIT_RECEIPT_NAME)

    tool_call_event = next(
        (event for event in audit_receipt["events"] if event.get("event_type") == "tool.called"),
        None,
    )
    assert tool_call_event is not None, "the audit chain has no tool.called event"

    # The field that would answer this does not exist today.
    AGENT_FIELDS = ["agent", "agent_id", "performed_by"]
    agent_field = next((field for field in AGENT_FIELDS if field in tool_call_event), None)

    assert agent_field, (
        f"the tool.called event carries no agent attribution; "
        f"event keys: {list(tool_call_event.keys())}"
    )
    # When the field exists, assert it matches the scenario constant.
    # Today this line is never reached.
    assert tool_call_event[agent_field] == scenario.AGENT_B


@pytest.mark.auditor_question(7)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "tool calls carry no identity attribution: the audit chain records "
        "tool.called events, but they do not name which identity was presented "
        "to the tool, so question 7 cannot be answered (#5057 harness landed; "
        "attribution producer work not yet assigned)"
    ),
)
def test_question7_identity_to_tool(auditor_bundle: BundleReader) -> None:
    """Q7: which identity was presented to the tool?

    The scenario's MCP tool call was made under some identity. Today's
    evidence does not carry it: the tool.called event records the tool
    name and server, but not the identity the agent presented to the tool.
    """
    audit_receipt = auditor_bundle.read_json(scenario.AUDIT_RECEIPT_NAME)

    tool_call_event = next(
        (event for event in audit_receipt["events"] if event.get("event_type") == "tool.called"),
        None,
    )
    assert tool_call_event is not None, "the audit chain has no tool.called event"

    # The field that would answer this does not exist today.
    IDENTITY_FIELDS = ["identity", "presented_identity", "caller_identity", "auth_identity"]
    identity_field = next((field for field in IDENTITY_FIELDS if field in tool_call_event), None)

    assert identity_field, (
        f"the tool.called event carries no identity attribution; "
        f"event keys: {list(tool_call_event.keys())}"
    )
    # When the field exists, assert its presence. The scenario does not
    # define a specific identity constant to compare against.
    # Today this line is never reached.
    assert tool_call_event[identity_field]


@pytest.mark.auditor_question(14)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "no receipt names the agent runtime environment: neither run-receipt.json "
        "nor audit-receipt.json carries code_version, config_hash, toolset or "
        "runtime_environment fields, so the bundle cannot say which code, config "
        "and tool set actually ran (#5057 harness landed; attribution producer "
        "work not yet assigned)"
    ),
)
def test_question14_code_config_toolset(auditor_bundle: BundleReader) -> None:
    """Q14: which agent code, config and tool set actually ran?

    The scenario's agents ran with some code version, config and tool set.
    Today's evidence does not carry it: neither receipt has a runtime
    environment section naming what actually ran.
    """
    audit_receipt = auditor_bundle.read_json(scenario.AUDIT_RECEIPT_NAME)
    run_receipt = auditor_bundle.read_json(scenario.RUN_RECEIPT_NAME)

    # The runtime environment field would live in one of these two receipts.
    RUNTIME_FIELDS = [
        "code_version",
        "config_hash",
        "toolset",
        "runtime_environment",
        "environment",
        "agent_version",
    ]

    runtime_in_audit = any(field in audit_receipt for field in RUNTIME_FIELDS)
    runtime_in_run = any(field in run_receipt for field in RUNTIME_FIELDS)

    assert runtime_in_audit or runtime_in_run, (
        f"neither receipt names the runtime environment; "
        f"audit_receipt keys: {list(audit_receipt.keys())}, "
        f"run_receipt keys: {list(run_receipt.keys())}"
    )
    # When the fields exist, assert their presence and structure.
    # Today this line is never reached.
    if runtime_in_audit:
        env = audit_receipt
    else:
        env = run_receipt

    runtime_field = next(field for field in RUNTIME_FIELDS if field in env)
    runtime_data = env[runtime_field]
    assert runtime_data, f"the {runtime_field} field exists but is empty"
