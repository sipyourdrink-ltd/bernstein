"""Audit-chain receipts for process-tree reaps (issue #2367).

Every forced termination of an agent process tree is mirrored into the
HMAC-chained audit log as a reap receipt: which platform mechanism delivered
the stop, whether escalation to a force-kill was required, and the grace
window that applied.  An operator reconstructing a failure window can prove
offline which reap path ran and on which platform semantics it relied.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_PROCESS_REAP_RECEIPT,
    AuditChainStore,
    record_process_reap_receipt,
)


def test_record_process_reap_receipt_appends_event(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    event = record_process_reap_receipt(
        chain=chain,
        session_id="agent-1",
        pgid=4321,
        os_name="linux",
        method="posix_process_group",
        delivered=True,
        escalated=False,
        grace_seconds=3.0,
        reason="kill_requested",
    )
    assert event.event_type == EVENT_PROCESS_REAP_RECEIPT
    rows = chain.query(event_type=EVENT_PROCESS_REAP_RECEIPT)
    assert len(rows) == 1
    details = rows[0].details
    assert details["session_id"] == "agent-1"
    assert details["pgid"] == 4321
    assert details["os_name"] == "linux"
    assert details["method"] == "posix_process_group"
    assert details["delivered"] is True
    assert details["escalated"] is False
    assert details["grace_seconds"] == 3.0
    assert details["reason"] == "kill_requested"
    assert "prev_chain_digest" in details


def test_record_process_reap_receipt_windows_method(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_process_reap_receipt(
        chain=chain,
        session_id="agent-2",
        pgid=999,
        os_name="windows",
        method="windows_process_tree",
        delivered=True,
        escalated=True,
        grace_seconds=3.0,
        reason="heartbeat_stale",
        actor="orchestrator",
    )
    rows = chain.query(event_type=EVENT_PROCESS_REAP_RECEIPT)
    assert rows[0].details["method"] == "windows_process_tree"
    assert rows[0].details["escalated"] is True
    assert rows[0].actor == "orchestrator"


def test_audit_chain_stays_verifiable_after_reap_receipt(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_process_reap_receipt(
        chain=chain,
        session_id="agent-1",
        pgid=4321,
        os_name="macos",
        method="posix_process_group",
        delivered=False,
        escalated=False,
        grace_seconds=3.0,
        reason="kill_requested",
    )
    ok, errors = chain.verify()
    assert ok, errors


def test_spawner_emit_helper_writes_receipt(tmp_path: Path) -> None:
    """The spawner-side emit helper mirrors a reap receipt best-effort."""
    from bernstein.core.platform_compat import ProcessReapReceipt

    from bernstein.core.agents.spawner_core import emit_process_reap_receipt

    (tmp_path / ".sdd").mkdir()
    receipt = ProcessReapReceipt(
        pgid=777,
        os_name="linux",
        method="posix_process_group",
        delivered=True,
        escalated=True,
        grace_seconds=3.0,
    )
    emit_process_reap_receipt(tmp_path, "sess-7", receipt, reason="kill_requested")

    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    rows = chain.query(event_type=EVENT_PROCESS_REAP_RECEIPT)
    assert len(rows) == 1
    assert rows[0].details["session_id"] == "sess-7"
    assert rows[0].details["pgid"] == 777
    assert rows[0].details["escalated"] is True


def test_spawner_emit_helper_never_raises(tmp_path: Path) -> None:
    """Audit mirroring must never mask the kill itself."""
    from unittest.mock import patch

    from bernstein.core.platform_compat import ProcessReapReceipt

    from bernstein.core.agents.spawner_core import emit_process_reap_receipt

    receipt = ProcessReapReceipt(
        pgid=1,
        os_name="linux",
        method="posix_process_group",
        delivered=False,
        escalated=False,
        grace_seconds=3.0,
    )
    with patch(
        "bernstein.core.security.audit_chain.AuditChainStore",
        side_effect=RuntimeError("audit backend down"),
    ):
        # Must not raise.
        emit_process_reap_receipt(tmp_path, "sess-8", receipt, reason="kill_requested")
