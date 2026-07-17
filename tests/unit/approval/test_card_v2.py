"""Approval card v2 envelope + chain-anchored gate (issue #2511).

Covers the acceptance criteria that make the card a hash-committed decision
record rather than a message with a log:

* determinism -- identical inputs / repo state -> byte-identical envelope,
* the envelope carries reasoning, impact estimate, rollback, and expiry,
* a decision echoing a mutated ``card_hash`` is refused and chain-recorded,
* expiry is enforced by the chain-side clock, including across a restart,
* offline reconstruction rebuilds the shown fields and detects mutation,
* a ``hard_one_way`` change carries an explicit, hashed irreversible marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.approval.card import (
    ApprovalCardV2,
    build_card,
    canonical_card_bytes,
    card_hash,
    render_card_text,
)
from bernstein.core.approval.card_gate import (
    ApprovalCardExpired,
    ApprovalCardGate,
    ApprovalCardHashMismatch,
)
from bernstein.core.security.audit_chain import (
    EVENT_APPROVAL_CARD_ISSUED,
    EVENT_APPROVAL_CARD_REFUSED,
    EVENT_APPROVAL_CARD_RESOLVED,
    AuditChainStore,
)

_KEY = b"deterministic-test-key-2511"


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


def _safe_card(*, created_at: float = 1_000.0, ttl: float = 600.0) -> ApprovalCardV2:
    return build_card(
        approval_id="ap-safe",
        tool_name="Edit",
        tool_args={"file_path": "src/app.py", "new_string": "x = 1"},
        reasoning="Add a constant used by the new endpoint.",
        created_at=created_at,
        ttl_seconds=ttl,
    )


def _destructive_card(*, created_at: float = 1_000.0, ttl: float = 600.0) -> ApprovalCardV2:
    return build_card(
        approval_id="ap-danger",
        tool_name="Bash",
        tool_args={"command": "rm -rf /var/data"},
        reasoning="Clear the stale data directory.",
        created_at=created_at,
        ttl_seconds=ttl,
    )


# ---------------------------------------------------------------------------
# Determinism (AC1)
# ---------------------------------------------------------------------------


def test_same_inputs_produce_byte_identical_envelope_and_hash() -> None:
    a = _safe_card()
    b = _safe_card()
    assert canonical_card_bytes(a) == canonical_card_bytes(b)
    assert card_hash(a) == card_hash(b)


def test_envelope_round_trips_through_dict_preserving_hash() -> None:
    card = _safe_card()
    restored = ApprovalCardV2.from_dict(card.to_dict())
    assert card_hash(restored) == card_hash(card)
    assert canonical_card_bytes(restored) == canonical_card_bytes(card)


def test_any_shown_field_change_changes_the_hash() -> None:
    base = _safe_card()
    # Mutating any operator-visible field must change the committed hash.
    mutated_reasoning = ApprovalCardV2.from_dict({**base.to_dict(), "reasoning": "totally different intent"})
    assert card_hash(mutated_reasoning) != card_hash(base)

    envelope = base.to_dict()
    envelope["impact"] = {**envelope["impact"], "score": envelope["impact"]["score"] + 0.5}
    mutated_impact = ApprovalCardV2.from_dict(envelope)
    assert card_hash(mutated_impact) != card_hash(base)


# ---------------------------------------------------------------------------
# Envelope content: reasoning + impact + rollback + expiry (AC content)
# ---------------------------------------------------------------------------


def test_envelope_carries_reasoning_impact_rollback_expiry() -> None:
    card = _safe_card(created_at=1_000.0, ttl=600.0)
    assert card.reasoning
    assert 0.0 <= card.impact.score <= 1.0
    assert card.rollback.procedure
    assert card.not_after == 1_600.0


def test_render_shows_impact_rollback_and_expiry() -> None:
    text = render_card_text(_safe_card())
    assert "Impact:" in text
    assert "Rollback:" in text
    assert "Expires at:" in text
    assert "Card hash:" in text


# ---------------------------------------------------------------------------
# hard_one_way -> irreversible marker inside the hashed envelope (AC7)
# ---------------------------------------------------------------------------


def test_hard_one_way_change_marks_irreversible_in_envelope_and_render() -> None:
    card = _destructive_card()
    assert card.impact.hard_one_way is True
    assert card.rollback.irreversible is True
    # The marker is part of the hashed envelope: flipping it changes the hash.
    flipped = ApprovalCardV2.from_dict(
        {**card.to_dict(), "rollback": {**card.to_dict()["rollback"], "irreversible": False}}
    )
    assert card_hash(flipped) != card_hash(card)
    assert "IRREVERSIBLE" in render_card_text(card)


# ---------------------------------------------------------------------------
# Gate: issue emits a chain event carrying the envelope + hash
# ---------------------------------------------------------------------------


def test_issue_appends_chain_event_with_envelope(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain, install_id="install-A", session_id="sess-1")
    card = _safe_card()
    issued = gate.issue(card, worktree_id="wt-a", thread_id="C42")

    events = chain.query(event_type=EVENT_APPROVAL_CARD_ISSUED)
    assert len(events) == 1
    details = events[0].details
    assert details["card_hash"] == issued.card_hash == card_hash(card)
    assert details["envelope"] == card.to_dict()
    assert details["worktree_id"] == "wt-a"
    assert "prev_chain_digest" in details
    ok, errors = chain.verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# Gate: hash-echo enforcement (AC2, verifiability)
# ---------------------------------------------------------------------------


def test_resolve_with_matching_hash_records_resolved_event(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain)
    card = _safe_card()
    issued = gate.issue(card)
    gate.resolve(card_hash=issued.card_hash, decision="approve", approver="U7", now=1_100.0)

    resolved = chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED)
    assert len(resolved) == 1
    assert resolved[0].details["card_hash"] == issued.card_hash
    assert resolved[0].details["decision"] == "approve"


def test_resolve_with_mutated_hash_is_refused_and_recorded(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain)
    card = _safe_card()
    gate.issue(card)

    # The operator "saw" a card whose impact score was tampered: its hash
    # differs from the issued envelope's hash.
    tampered = ApprovalCardV2.from_dict({**card.to_dict(), "impact": {**card.to_dict()["impact"], "score": 0.99}})
    tampered_hash = card_hash(tampered)
    assert tampered_hash != card_hash(card)

    with pytest.raises(ApprovalCardHashMismatch):
        gate.resolve(card_hash=tampered_hash, decision="approve", approver="U7", now=1_100.0)

    # No resolution happened; a refusal is chain-recorded so the tool call
    # provably did not proceed.
    assert chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED) == []
    refused = chain.query(event_type=EVENT_APPROVAL_CARD_REFUSED)
    assert len(refused) == 1
    assert refused[0].details["reason"] == "hash_mismatch"
    ok, errors = chain.verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# Gate: chain-side expiry enforcement, including across a restart (AC3)
# ---------------------------------------------------------------------------


def test_resolve_after_not_after_is_refused(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    gate = ApprovalCardGate(chain)
    card = _safe_card(created_at=1_000.0, ttl=600.0)  # not_after == 1600
    issued = gate.issue(card)

    with pytest.raises(ApprovalCardExpired):
        gate.resolve(card_hash=issued.card_hash, decision="approve", now=1_600.0)

    assert chain.query(event_type=EVENT_APPROVAL_CARD_RESOLVED) == []
    refused = chain.query(event_type=EVENT_APPROVAL_CARD_REFUSED)
    assert len(refused) == 1
    assert refused[0].details["reason"] == "expired"


def test_expiry_enforced_after_chat_process_restart(tmp_path: Path) -> None:
    # Issue on one gate/process.
    chain1 = _chain(tmp_path)
    gate1 = ApprovalCardGate(chain1)
    card = _safe_card(created_at=1_000.0, ttl=600.0)
    issued = gate1.issue(card)

    # A fresh gate over a fresh chain store on the same audit dir models a
    # chat-process restart: it holds no in-memory issued cards. It must still
    # refuse the expired approve by reconstructing the envelope from the chain.
    chain2 = _chain(tmp_path)
    gate2 = ApprovalCardGate(chain2)
    with pytest.raises(ApprovalCardExpired):
        gate2.resolve(card_hash=issued.card_hash, decision="approve", now=1_700.0)

    refused = chain2.query(event_type=EVENT_APPROVAL_CARD_REFUSED)
    assert len(refused) == 1
    assert refused[0].details["reason"] == "expired"


def test_resolve_still_works_after_restart_before_expiry(tmp_path: Path) -> None:
    chain1 = _chain(tmp_path)
    gate1 = ApprovalCardGate(chain1)
    issued = gate1.issue(_safe_card(created_at=1_000.0, ttl=600.0))

    chain2 = _chain(tmp_path)
    gate2 = ApprovalCardGate(chain2)
    # Before not_after, a reconstructed card resolves cleanly.
    resolved = gate2.resolve(card_hash=issued.card_hash, decision="approve", now=1_200.0)
    assert resolved.card_hash == issued.card_hash
    assert chain2.query(event_type=EVENT_APPROVAL_CARD_RESOLVED)
