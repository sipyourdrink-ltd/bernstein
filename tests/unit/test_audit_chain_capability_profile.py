"""Tests for the additive capability-routing audit-chain helpers (issue #2663).

Capability-aware adapter selection produces two decisions worth proving from
the chain alone:

* the content-addressed capability profile an adapter presented when it was
  chosen to run a task, so replay detects a changed declaration as a hash
  divergence named by the adapter (``adapter.capability_selection``);
* the content-addressed refusal receipt emitted when no candidate satisfied a
  task, so a routing refusal is a signed record rather than a silent fallback
  (``adapter.capability_refusal``).

Both events embed the previous chain digest and record only names and hashes --
never a prompt or a spawn command.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_ADAPTER_CAPABILITY_REFUSAL,
    EVENT_ADAPTER_CAPABILITY_SELECTION,
    AuditChainStore,
    record_capability_refusal,
    record_capability_selection,
)


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def test_record_capability_selection_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_capability_selection(
        chain=chain,
        run_id="run-1",
        adapter="pydantic_ai",
        profile_hash="ab" * 32,
        requirements={"mcp_client": True, "sandbox": "process"},
    )
    assert event.event_type == EVENT_ADAPTER_CAPABILITY_SELECTION
    assert event.resource_id == "pydantic_ai"
    assert "prev_chain_digest" in event.details
    assert event.details["run_id"] == "run-1"
    assert event.details["adapter"] == "pydantic_ai"
    assert event.details["profile_hash"] == "ab" * 32
    assert event.details["requirements"] == {"mcp_client": True, "sandbox": "process"}
    ok, errors = chain.verify()
    assert ok, errors


def test_record_capability_refusal_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_capability_refusal(
        chain=chain,
        run_id="run-1",
        receipt_hash="cd" * 32,
        requirements={"vision": True},
        candidates=[["droid", "11" * 32], ["kimi", "22" * 32]],
        unmet=["vision"],
    )
    assert event.event_type == EVENT_ADAPTER_CAPABILITY_REFUSAL
    # The refusal is addressed by the receipt hash: the identity of the artefact.
    assert event.resource_id == "cd" * 32
    assert "prev_chain_digest" in event.details
    assert event.details["run_id"] == "run-1"
    assert event.details["receipt_hash"] == "cd" * 32
    assert event.details["requirements"] == {"vision": True}
    assert event.details["candidates"] == [["droid", "11" * 32], ["kimi", "22" * 32]]
    assert event.details["unmet"] == ["vision"]
    ok, errors = chain.verify()
    assert ok, errors


def test_capability_events_chain_together(tmp_path: Path) -> None:
    """A refusal followed by a selection stays a single verifiable chain."""
    chain = _store(tmp_path)
    first = record_capability_refusal(
        chain=chain,
        run_id="run-1",
        receipt_hash="cd" * 32,
        requirements={"vision": True},
        candidates=[["droid", "11" * 32]],
        unmet=["vision"],
    )
    second = record_capability_selection(
        chain=chain,
        run_id="run-1",
        adapter="droid",
        profile_hash="11" * 32,
        requirements={},
    )
    # The second event chains onto the first: its embedded predecessor is not
    # the genesis digest the first embedded.
    assert second.details["prev_chain_digest"] != first.details["prev_chain_digest"]
    ok, errors = chain.verify()
    assert ok, errors
