"""Attribution questions answered only from the exported bundle (#5058)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conformance.auditor import recorder

if TYPE_CHECKING:
    from tests.conformance.auditor.bundle import BundleReader


@pytest.mark.question(1)
def test_q1_the_record_names_the_initiating_principal(bundle_reader: BundleReader) -> None:
    """Q1: the exported bundle identifies the human or principal who started the run."""
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
    """Q2: each action in the journal and spine records which agent performed it."""
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    action_events = [
        e
        for e in run["journal"]["events"]
        if e["event"] in ("tool_called", "file_read", "model_request", "artifact_written")
    ]
    assert action_events, "no action events recorded in journal"
    for e in action_events:
        assert "agent_id" in e, f"event {e['event']} missing agent_id"
        assert e["agent_id"] in ("agent-a", "agent-b")

    spine_entries = run["spine"]["entries"]
    assert spine_entries, "spine has no entries"
    for entry in spine_entries:
        assert entry.get("actor") == "agent-a"


@pytest.mark.question(7)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "the exported bundle has no per-tool-call identity record binding the "
        "acting agent to the identity presented to the tool server (#5058)"
    ),
)
def test_q7_the_identity_presented_to_the_tool_is_recorded(bundle_reader: BundleReader) -> None:
    """Q7: which identity was presented to the tool server during tool calls."""
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    tool_calls = [e for e in run["journal"]["events"] if e["event"] == "tool_called"]
    assert tool_calls, "no tool_called event in journal"
    for call in tool_calls:
        assert call.get("presented_identity"), "journal tool_called missing presented_identity (#5058)"

    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    audit_calls = [e for e in audit["events"] if e["event_type"] == "tool.called"]
    assert audit_calls, "no tool.called event in audit receipt"
    for call in audit_calls:
        assert call["details"].get("presented_identity"), "audit tool.called missing presented_identity (#5058)"


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
