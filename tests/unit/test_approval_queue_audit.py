"""A person's approval decision must be as recorded as the classifier's.

Covers the audit-chain write added to ``ApprovalQueue.resolve``
(``src/bernstein/core/approval/queue.py``).

The classifier already recorded every auto-approve and auto-deny through
``gate.py``'s ``_record_classifier_decision``. ``resolve()`` -- the function
that applies a human's allow/reject/always -- contained no chain write at all.
So the least accountable decision source was the most durably recorded, and
the decision that most needs attribution, a person permitting a gated action,
left nothing in the tamper-evident chain (#4536).

The queue's own resolved files are nonce-protected, but they sit outside the
chain: they can be edited without breaking anything a verifier checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.approval.models import ApprovalDecision, PendingApproval
from bernstein.core.approval.queue import ApprovalQueue
from bernstein.core.security.audit import AuditLog


def _queue(root: Path) -> ApprovalQueue:
    return ApprovalQueue(base_dir=root / ".sdd" / "runtime" / "approvals")


def _audit(root: Path) -> AuditLog:
    return AuditLog(audit_dir=root / ".sdd" / "audit")


def _events(root: Path, event_type: str) -> list:
    return [e for e in _audit(root).query() if e.event_type == event_type]


def _enqueue(queue: ApprovalQueue, tool: str = "Bash") -> tuple[str, bytes]:
    pending = queue.push(
        PendingApproval(
            session_id="S-1",
            agent_role="backend",
            tool_name=tool,
            tool_args={"command": "rm -rf /tmp/x"},
        )
    )
    return pending.id, pending.nonce


def test_human_allow_appends_audit_chain_event(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    approval_id, nonce = _enqueue(queue)

    queue.resolve(approval_id, ApprovalDecision.ALLOW, nonce=nonce, channel="cli")

    events = _events(tmp_path, "human_approval_decision")
    assert events, "a human allow left no chain entry"
    assert events[0].details["approval_id"] == approval_id
    assert events[0].details["decision"] == "allow"
    assert events[0].details["channel"] == "cli"


def test_human_reject_is_recorded_too(tmp_path: Path) -> None:
    """A denial is the half an operator is most likely to be asked about."""
    queue = _queue(tmp_path)
    approval_id, nonce = _enqueue(queue)

    queue.resolve(approval_id, ApprovalDecision.REJECT, nonce=nonce, channel="http")

    events = _events(tmp_path, "human_approval_decision")
    assert [e.details["decision"] for e in events] == ["reject"]


def test_the_chain_verifies_after_a_human_decision(tmp_path: Path) -> None:
    """The new entry must chain correctly, not merely be present."""
    queue = _queue(tmp_path)
    approval_id, nonce = _enqueue(queue)
    queue.resolve(approval_id, ApprovalDecision.ALLOW, nonce=nonce)

    valid, errors = _audit(tmp_path).verify()

    assert valid, f"audit chain did not verify: {errors}"


def test_human_and_classifier_decisions_are_distinguishable(tmp_path: Path) -> None:
    """An auditor must be able to separate 'a person allowed this' from 'a rule did'."""
    queue = _queue(tmp_path)
    approval_id, nonce = _enqueue(queue)
    queue.resolve(approval_id, ApprovalDecision.ALLOW, nonce=nonce)

    human = _events(tmp_path, "human_approval_decision")
    auto = _events(tmp_path, "auto_approve_decision")

    assert human and not auto
    assert human[0].details["decision_source"] == "human"
    assert human[0].actor == "human"


def test_always_allow_promotion_is_its_own_event(tmp_path: Path) -> None:
    """ALWAYS changes future behaviour, so it is a separate recorded fact.

    Folding it into the allow as an adjective would mean the moment the
    queue stopped being consulted for a pattern is not itself a chain event.
    """
    queue = _queue(tmp_path)
    approval_id, nonce = _enqueue(queue)

    queue.resolve(approval_id, ApprovalDecision.ALWAYS, nonce=nonce)

    decisions = _events(tmp_path, "human_approval_decision")
    promotions = _events(tmp_path, "always_allow_promotion")
    assert len(decisions) == 1
    assert len(promotions) == 1
    assert promotions[0].details["approval_id"] == approval_id


def test_a_plain_allow_creates_no_promotion_event(tmp_path: Path) -> None:
    """The pairing: promotion must mean promotion, not merely approval."""
    queue = _queue(tmp_path)
    approval_id, nonce = _enqueue(queue)

    queue.resolve(approval_id, ApprovalDecision.ALLOW, nonce=nonce)

    assert _events(tmp_path, "always_allow_promotion") == []


def test_channel_is_unspecified_rather_than_invented(tmp_path: Path) -> None:
    """Attribution records what is known. A guessed identity would be worse
    than a quiet one, because the chain cannot mark it as a guess."""
    queue = _queue(tmp_path)
    approval_id, nonce = _enqueue(queue)

    queue.resolve(approval_id, ApprovalDecision.ALLOW, nonce=nonce)

    details = _events(tmp_path, "human_approval_decision")[0].details
    assert details["channel"] == "unspecified"
    assert "user" not in details and "identity" not in details


def test_chain_write_failure_does_not_leave_queue_inconsistent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply-then-record: a broken audit directory must not strand the operator.

    Recording first would let a chain entry assert a decision that then failed
    to apply, and a verifier cannot tell such an entry from a true one. A
    missing entry is the safer failure -- the resolved file exists without a
    matching event, which is detectable.
    """
    queue = _queue(tmp_path)
    approval_id, nonce = _enqueue(queue)

    import bernstein.core.security.audit as audit_mod

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("audit directory is not writable")

    monkeypatch.setattr(audit_mod.AuditLog, "log", _boom)

    resolution = queue.resolve(approval_id, ApprovalDecision.REJECT, nonce=nonce)

    # The decision still applied and is durable.
    assert resolution.decision is ApprovalDecision.REJECT
    assert queue.get_resolution(approval_id) is not None
    assert approval_id not in {p.id for p in queue.list_pending()}
    # And it survives a reload from disk.
    assert _queue(tmp_path).get_resolution(approval_id) is not None
