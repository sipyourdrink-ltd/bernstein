"""Attribution questions answered only from the exported bundle (#5058)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conformance.auditor import recorder

if TYPE_CHECKING:
    from tests.conformance.auditor.bundle import BundleReader


@pytest.mark.question(1)
def test_q1_the_record_names_the_initiating_principal(bundle_reader: BundleReader) -> None:
    """Q1: the exported bundle identifies the human or principal who started the run.

    This test passes because the scripted recorder fixture populates ``principal``
    and ``actor`` from ``recorder.py:88-114``; the orchestrator's own
    ``run_started`` event (``orchestrator.py:3173-3181``) does not yet carry a
    principal field in production runs.  The test validates the *bundle schema*
    — that the field can be written and round-tripped — not that every production
    run will populate it.
    """
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    start_events = [e for e in run["journal"]["events"] if e["event"] == "run_started"]
    assert start_events, "run_started event missing from journal"
    assert start_events[0]["principal"] == "user:operator@example.org"

    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    start_audit = [e for e in audit["events"] if e["event_type"] == "run.started"]
    assert start_audit, "run.started event missing from audit receipt"
    assert start_audit[0]["actor"] == "user:operator@example.org"


@pytest.mark.question(2)
def test_q2_the_record_names_the_agent_for_each_recorded_action(bundle_reader: BundleReader) -> None:
    """Q2: each action in the journal and spine records which agent performed it.

    This test passes because the scripted recorder fixture populates ``agent_id``
    and ``actor`` explicitly (``recorder.py:88-114``).  The two agent IDs in the
    fixture are ``agent-a`` and ``agent-b``; the spine only records ``agent-a``
    actions in the bundled scenario.  This validates the bundle *schema*, not that
    every production writer populates these fields consistently.
    """
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    agent_ids = {"agent-a", "agent-b"}
    action_events = [
        e
        for e in run["journal"]["events"]
        if e["event"] in ("tool_called", "file_read", "model_request", "artifact_written")
    ]
    assert action_events, "no action events recorded in journal"
    for e in action_events:
        assert "agent_id" in e, f"event {e['event']} missing agent_id"
        assert e["agent_id"] in agent_ids

    spine_entries = run["spine"]["entries"]
    assert spine_entries, "spine has no entries"
    for entry in spine_entries:
        assert entry.get("actor") in agent_ids


@pytest.mark.question(7)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "the exported bundle does not carry a ToolCallIdentityAttestation record "
        "per tool call; the gateway today writes actor='mcp_gateway' for every call "
        "(core/protocols/mcp/mcp_gateway.py:392) and the attestation fields "
        "(args_digest, intent_digest, identity_anchor_ref, tool_signing_kid) "
        "defined in core/security/toolcall_identity.py:26-45 do not reach the "
        "bundle (#5058)"
    ),
)
def test_q7_the_identity_presented_to_the_tool_is_recorded(bundle_reader: BundleReader) -> None:
    """Q7: which identity was presented to the tool server during tool calls.

    When ToolCallIdentityAttestation reaches the bundle, each tool_called event
    in the journal and audit receipt should carry the attestation fields defined
    in ``core/security/toolcall_identity.py:26-45``:
    ``args_digest``, ``intent_digest``, ``identity_anchor_ref``, ``tool_signing_kid``.
    """
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    tool_calls = [e for e in run["journal"]["events"] if e["event"] == "tool_called"]
    assert tool_calls, "no tool_called event in journal"
    for call in tool_calls:
        # These fields are defined in ToolCallIdentityAttestation (toolcall_identity.py:39-45)
        assert call.get("args_digest"), "journal tool_called missing args_digest (#5058)"
        assert call.get("intent_digest"), "journal tool_called missing intent_digest (#5058)"
        assert call.get("identity_anchor_ref"), "journal tool_called missing identity_anchor_ref (#5058)"
        assert call.get("tool_signing_kid"), "journal tool_called missing tool_signing_kid (#5058)"

    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    audit_calls = [e for e in audit["events"] if e["event_type"] == "tool.called"]
    assert audit_calls, "no tool.called event in audit receipt"
    for call in audit_calls:
        details = call.get("details", {})
        assert details.get("args_digest"), "audit tool.called missing args_digest (#5058)"
        assert details.get("intent_digest"), "audit tool.called missing intent_digest (#5058)"
        assert details.get("identity_anchor_ref"), "audit tool.called missing identity_anchor_ref (#5058)"
        assert details.get("tool_signing_kid"), "audit tool.called missing tool_signing_kid (#5058)"


@pytest.mark.question(14)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "the exported bundle does not bind the agent identity to a code, config "
        "and tool set digest for the agent that ran (#5058)"
    ),
)
def test_q14_the_agent_code_config_and_toolset_digest_is_recorded(bundle_reader: BundleReader) -> None:
    """Q14: which agent code, config and tool set actually ran."""
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    spawns = [e for e in run["journal"]["events"] if e["event"] == "agent_spawned"]
    assert spawns, "no agent_spawned events in journal"
    for spawn in spawns:
        assert spawn.get("code_digest"), "agent_spawned missing code_digest (#5058)"
        assert spawn.get("config_digest"), "agent_spawned missing config_digest (#5058)"
        assert spawn.get("toolset_digest"), "agent_spawned missing toolset_digest (#5058)"
