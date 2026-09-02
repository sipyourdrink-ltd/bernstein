"""Audit-chain recording of MCP capability drift events (#7937).

When an MCP server's declared tool set changes, the drift is mirrored into
the HMAC-chained audit log as a tamper-evident entry. This module pins the
recording contract:

* ``record_mcp_capability_drift`` appends an event whose ``details`` carries
  the server name, current/previous digest, added/removed tool sets, and
  tool count.
* The chain entry is queryable and correlates by ``current_digest``.
* ``prev_chain_digest`` is set on every entry so the chain remains linked.
* A byte-flip in a recorded details payload breaks ``chain.verify()``.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_MCP_CAPABILITY_DRIFT,
    AuditChainStore,
    MCPCapabilityDriftDetails,
    record_mcp_capability_drift,
)

KEY = b"k" * 32


def _create_chain(tmp_path: Path) -> AuditChainStore:
    """Return a fresh ``AuditChainStore`` over an isolated temp dir."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return AuditChainStore(audit_dir, key=KEY)


def test_first_contact_no_previous_digest(tmp_path: Path) -> None:
    """First contact records ``previous_digest`` as ``None``."""
    chain = _create_chain(tmp_path)

    event = record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_a", "tool_b"),
    )

    assert event.event_type == EVENT_MCP_CAPABILITY_DRIFT
    assert event.actor == "test-server"
    assert event.resource_type == "mcp_capability_drift"
    assert event.resource_id.startswith("sha256:")

    details = event.details
    assert details["server_name"] == "test-server"
    assert details["previous_digest"] is None
    assert details["current_digest"].startswith("sha256:")
    assert details["added_tools"] == ["tool_a", "tool_b"]
    assert details["removed_tools"] == []
    assert details["tool_count"] == 2
    assert "prev_chain_digest" in details


def test_drift_with_previous_tools_records_added_and_removed(tmp_path: Path) -> None:
    """Subsequent call with different tools records added and removed."""
    chain = _create_chain(tmp_path)

    record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_a", "tool_b"),
    )
    event = record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_b", "tool_c"),
        previous_tools=("tool_a", "tool_b"),
    )

    details = event.details
    assert details["added_tools"] == ["tool_c"]
    assert details["removed_tools"] == ["tool_a"]
    assert details["tool_count"] == 2
    # previous_digest should match the first call's current_digest
    prev_events = chain.query(event_type=EVENT_MCP_CAPABILITY_DRIFT)
    assert len(prev_events) == 2
    assert details["previous_digest"] == prev_events[0].resource_id


def test_identical_tools_no_drift(tmp_path: Path) -> None:
    """Same tool list produces empty added/removed sets."""
    chain = _create_chain(tmp_path)

    event = record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_a", "tool_b"),
        previous_tools=("tool_a", "tool_b"),
    )

    details = event.details
    assert details["added_tools"] == []
    assert details["removed_tools"] == []
    assert details["tool_count"] == 2


def test_digest_is_deterministic(tmp_path: Path) -> None:
    """Same tool list produces the same digest across chains."""
    chain_a = _create_chain(tmp_path / "a")
    chain_b = _create_chain(tmp_path / "b")

    event_a = record_mcp_capability_drift(
        chain=chain_a,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_a", "tool_b", "tool_c"),
    )
    event_b = record_mcp_capability_drift(
        chain=chain_b,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_a", "tool_b", "tool_c"),
    )

    assert event_a.details["current_digest"] == event_b.details["current_digest"]


def test_digest_changes_with_tool_order(tmp_path: Path) -> None:
    """Different tool sets produce different digests even if same count."""
    chain = _create_chain(tmp_path)

    event_a = record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_a", "tool_b"),
    )
    event_b = record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_c", "tool_d"),
    )

    assert event_a.details["current_digest"] != event_b.details["current_digest"]


def test_event_is_queryable(tmp_path: Path) -> None:
    """The drift event is retrievable via ``chain.query``."""
    chain = _create_chain(tmp_path)
    record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_a",),
    )

    events = chain.query(event_type=EVENT_MCP_CAPABILITY_DRIFT)
    assert len(events) == 1
    assert events[0].details["server_name"] == "test-server"


def test_prev_chain_digest_is_set(tmp_path: Path) -> None:
    """``prev_chain_digest`` chains events onto each other."""
    chain = _create_chain(tmp_path)

    event1 = record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_a",),
    )
    event2 = record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_b",),
        previous_tools=("tool_a",),
    )

    assert event1.details["prev_chain_digest"] != ""
    assert event2.details["prev_chain_digest"] != ""
    assert event2.details["prev_chain_digest"] == event1.hmac


def test_tamper_evidence_byte_flip_breaks_verification(tmp_path: Path) -> None:
    """A byte-flip in a recorded details payload must break ``chain.verify()``."""
    chain = _create_chain(tmp_path)
    record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="test-server",
        current_tools=("tool_a",),
    )

    target = sorted(chain._log._audit_dir.glob("*.jsonl"))[0]  # pyright: ignore[reportPrivateUsage]
    raw = target.read_bytes()

    needle = b'"server_name": "test-server"'
    offset = raw.find(needle)
    assert offset > 0

    mutated = bytearray(raw)
    char_offset = offset + len(b'"server_name": "test-')
    mutated[char_offset] ^= 0x01
    target.write_bytes(bytes(mutated))

    ok, errors = chain.verify()
    assert not ok
    assert errors


def test_details_dataclass_to_dict(tmp_path: Path) -> None:
    """``MCPCapabilityDriftDetails.to_dict`` round-trips all fields."""
    details = MCPCapabilityDriftDetails(
        run_id="test-run-123",
        server_name="test-server",
        previous_digest="sha256:" + "a" * 64,
        current_digest="sha256:" + "b" * 64,
        added_tools=("tool_c",),
        removed_tools=("tool_a",),
        tool_count=1,
    )
    d = details.to_dict()
    assert d == {
        "run_id": "test-run-123",
        "server_name": "test-server",
        "previous_digest": "sha256:" + "a" * 64,
        "current_digest": "sha256:" + "b" * 64,
        "added_tools": ["tool_c"],
        "removed_tools": ["tool_a"],
        "tool_count": 1,
    }


def test_clean_chain_verifies(tmp_path: Path) -> None:
    """A clean chain with drift events must verify."""
    chain = _create_chain(tmp_path)
    record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="server-1",
        current_tools=("tool_a", "tool_b"),
    )
    record_mcp_capability_drift(
        chain=chain,
        run_id="test-run-123",
        server_name="server-1",
        current_tools=("tool_a", "tool_c"),
        previous_tools=("tool_a", "tool_b"),
    )

    ok, errors = chain.verify()
    assert ok, errors
    assert errors == []
