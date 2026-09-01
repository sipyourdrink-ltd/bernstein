"""Unit tests for the supervisor escalation-receipt module (#1800)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.orchestration.supervisor_receipt import (
    DEFAULT_RECEIPT_AUDIT_WINDOW,
    CrossWorktreeFenceError,
    IdentityTokens,
    RecommendedAction,
    StallReason,
    assemble_receipt,
    assert_cross_worktree_fence,
    canonical_receipt_bytes,
    receipt_from_dict,
    receipt_to_dict,
    recommend_action,
    sign_receipt,
    verify_receipt,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _identity() -> IdentityTokens:
    return IdentityTokens(
        install_rev="abc1234567890def",
        keyid="0" * 64,
        run_id="run-1",
    )


def _audit_slice(*, with_fatal: bool = False) -> list[dict[str, str]]:
    base = [
        {"event_type": "session.start", "session_id": "sess-1"},
        {"event_type": "task.pre_spawn", "session_id": "sess-1"},
        {"event_type": "heartbeat.tick", "session_id": "sess-1"},
    ]
    if with_fatal:
        base.append({"event_type": "auth.denied", "session_id": "sess-1"})
    return base


# ---------------------------------------------------------------------------
# recommend_action - deterministic contract
# ---------------------------------------------------------------------------


def test_recommended_action_for_park_when_respawn_exhausted() -> None:
    """RESPAWN_EXHAUSTED always yields PARK regardless of slice contents."""
    action = recommend_action(StallReason.RESPAWN_EXHAUSTED, _audit_slice())
    assert action == RecommendedAction.PARK


def test_recommended_action_escalate_on_fatal_auth_event() -> None:
    """A fatal-looking event anywhere in the slice short-circuits to ESCALATE."""
    action = recommend_action(
        StallReason.HEARTBEAT_STALE,
        _audit_slice(with_fatal=True),
        respawn_budget_remaining=3,
    )
    assert action == RecommendedAction.ESCALATE


def test_recommended_action_respawn_when_budget_remains() -> None:
    """HEARTBEAT_STALE with budget remaining and clean slice yields RESPAWN."""
    action = recommend_action(
        StallReason.HEARTBEAT_STALE,
        _audit_slice(),
        respawn_budget_remaining=2,
    )
    assert action == RecommendedAction.RESPAWN


def test_recommended_action_escalate_when_budget_exhausted_but_not_parked() -> None:
    """Heartbeat stall without budget escalates rather than silently parking."""
    action = recommend_action(
        StallReason.HEARTBEAT_STALE,
        _audit_slice(),
        respawn_budget_remaining=0,
    )
    assert action == RecommendedAction.ESCALATE


def test_recommended_action_inspect_for_unknown_reason() -> None:
    """Unknown stall reasons must never silently downgrade to RESPAWN."""
    action = recommend_action(StallReason.UNKNOWN, _audit_slice())
    assert action == RecommendedAction.INSPECT


def test_recommended_action_escalate_for_model_question() -> None:
    """A model question never auto-recovers - escalate to operator."""
    action = recommend_action(StallReason.WATCHDOG_MODEL_QUESTION, _audit_slice())
    assert action == RecommendedAction.ESCALATE


def test_recommended_action_escalate_for_manager_no_children() -> None:
    """Manager-no-children stalls always escalate (a respawn won't help)."""
    action = recommend_action(StallReason.MANAGER_NO_CHILDREN, _audit_slice())
    assert action == RecommendedAction.ESCALATE


def test_recommended_action_inspect_for_cohort_laggard() -> None:
    """Cohort-laggard stalls require human inspection before respawn."""
    action = recommend_action(StallReason.COHORT_LAGGARD, _audit_slice())
    assert action == RecommendedAction.INSPECT


def test_recommended_action_string_reason_coerced() -> None:
    """Raw string reasons are coerced into the StallReason enum."""
    action = recommend_action("respawn_budget_exhausted", _audit_slice())
    assert action == RecommendedAction.PARK


def test_recommended_action_unknown_string_coerced_to_unknown() -> None:
    """An unrecognised string reason coerces to UNKNOWN -> INSPECT."""
    action = recommend_action("totally-made-up", _audit_slice())
    assert action == RecommendedAction.INSPECT


def test_recommended_action_determinism(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Two operators in different temp dirs see byte-identical recommendations.

    Drives the same canonical chain slice through ``recommend_action`` from
    two distinct working directories. The function is pure over its inputs,
    so the byte representation of the returned enum value MUST match
    irrespective of host state, environment, or filesystem layout.
    """
    dir_a = tmp_path_factory.mktemp("op_a")
    dir_b = tmp_path_factory.mktemp("op_b")

    # Persist the slice to JSON files in two different temp dirs and
    # reload from each - this is the operator-side workflow.
    slice_in = _audit_slice(with_fatal=True)
    path_a = dir_a / "slice.json"
    path_b = dir_b / "slice.json"
    path_a.write_text(json.dumps(slice_in, sort_keys=True))
    path_b.write_text(json.dumps(slice_in, sort_keys=True))

    loaded_a = json.loads(path_a.read_text())
    loaded_b = json.loads(path_b.read_text())

    action_a = recommend_action(
        StallReason.HEARTBEAT_STALE,
        loaded_a,
        respawn_budget_remaining=2,
    )
    action_b = recommend_action(
        StallReason.HEARTBEAT_STALE,
        loaded_b,
        respawn_budget_remaining=2,
    )

    assert action_a == action_b
    # Byte-identical when encoded - protects against future enum churn
    # that might keep equality but change the .value string.
    assert action_a.value.encode() == action_b.value.encode()


# ---------------------------------------------------------------------------
# Cross-worktree fence
# ---------------------------------------------------------------------------


def test_cross_worktree_fence_passes_on_clean_slice() -> None:
    """Clean slice has no cross-worktree resolution events."""
    assert_cross_worktree_fence("sess-1", "wt-A", _audit_slice())


def test_cross_worktree_fence_flags_sibling_resolution_event() -> None:
    """A ``*.resolved`` event in a sibling worktree fails the fence."""
    slice_in = [
        *_audit_slice(),
        {
            "event_type": "task.resolved",
            "session_id": "sess-1",
            "worktree_id": "wt-B",
        },
    ]
    with pytest.raises(CrossWorktreeFenceError, match="cross-worktree fence"):
        assert_cross_worktree_fence("sess-1", "wt-A", slice_in)


def test_cross_worktree_fence_ignores_same_worktree_resolution() -> None:
    """A resolution event inside the stuck worker's own worktree is fine."""
    slice_in = [
        *_audit_slice(),
        {
            "event_type": "task.resolved",
            "session_id": "sess-1",
            "worktree_id": "wt-A",
        },
    ]
    assert_cross_worktree_fence("sess-1", "wt-A", slice_in)


def test_cross_worktree_fence_ignores_other_session_resolution() -> None:
    """A sibling worktree resolving a different session is irrelevant."""
    slice_in = [
        *_audit_slice(),
        {
            "event_type": "task.resolved",
            "session_id": "sess-99",
            "worktree_id": "wt-B",
        },
    ]
    assert_cross_worktree_fence("sess-1", "wt-A", slice_in)


# ---------------------------------------------------------------------------
# Receipt assembly + signature roundtrip
# ---------------------------------------------------------------------------


def _signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def test_assemble_receipt_trims_audit_window() -> None:
    """Audit slice is capped at DEFAULT_RECEIPT_AUDIT_WINDOW trailing entries."""
    over_window = DEFAULT_RECEIPT_AUDIT_WINDOW + 8
    audit_entries = [{"event_type": "noop", "session_id": "sess-1", "details": {"i": i}} for i in range(over_window)]
    receipt = assemble_receipt(
        worker_id="abc123",
        worktree_id="wt-A",
        session_id="sess-1",
        stall_reason=StallReason.HEARTBEAT_STALE,
        audit_entries=audit_entries,
        identity=_identity(),
        prev_chain_digest="0" * 64,
        respawn_budget_remaining=1,
    )
    assert len(receipt.audit_entries) == DEFAULT_RECEIPT_AUDIT_WINDOW
    # The trailing N entries are kept (highest indices).
    last_kept = receipt.audit_entries[-1]
    assert last_kept["details"]["i"] == over_window - 1


def test_sign_and_verify_roundtrip(tmp_path: Path) -> None:
    """A signed receipt verifies against the matching public key."""
    key = _signing_key()
    receipt = assemble_receipt(
        worker_id="abc123",
        worktree_id="wt-A",
        session_id="sess-1",
        stall_reason=StallReason.HEARTBEAT_STALE,
        audit_entries=_audit_slice(),
        identity=_identity(),
        prev_chain_digest="0" * 64,
        respawn_budget_remaining=2,
    )
    signed = sign_receipt(receipt, signing_key=key)
    result = verify_receipt(signed, key.public_key())
    assert result.ok, result.errors

    # Roundtrip through dict <-> dataclass and verify again.
    blob = json.dumps(receipt_to_dict(signed), sort_keys=True)
    reloaded = receipt_from_dict(json.loads(blob))
    result2 = verify_receipt(reloaded, key.public_key())
    assert result2.ok, result2.errors


def test_verify_rejects_signature_tampering() -> None:
    """Tampered signature bytes fail verification."""
    key = _signing_key()
    receipt = assemble_receipt(
        worker_id="abc123",
        worktree_id="wt-A",
        session_id="sess-1",
        stall_reason=StallReason.HEARTBEAT_STALE,
        audit_entries=_audit_slice(),
        identity=_identity(),
        prev_chain_digest="0" * 64,
        respawn_budget_remaining=2,
    )
    signed = sign_receipt(receipt, signing_key=key)
    tampered_dict = receipt_to_dict(signed)
    # Flip a base64 byte that doesn't break decode but breaks the signature.
    tampered_dict["signature_b64"] = tampered_dict["signature_b64"][:-2] + (
        "AA" if tampered_dict["signature_b64"][-2:] != "AA" else "BB"
    )
    tampered = receipt_from_dict(tampered_dict)
    result = verify_receipt(tampered, key.public_key())
    assert not result.ok


def test_verify_rejects_recommended_action_swap() -> None:
    """A tampered recommended_action fails the determinism re-derivation."""
    key = _signing_key()
    receipt = assemble_receipt(
        worker_id="abc123",
        worktree_id="wt-A",
        session_id="sess-1",
        stall_reason=StallReason.HEARTBEAT_STALE,
        audit_entries=_audit_slice(with_fatal=True),
        identity=_identity(),
        prev_chain_digest="0" * 64,
        respawn_budget_remaining=2,
    )
    signed = sign_receipt(receipt, signing_key=key)
    tampered_dict = receipt_to_dict(signed)
    # Swap ESCALATE for RESPAWN - signature still covers the original
    # bytes, but the determinism check catches the mismatch.
    tampered_dict["recommended_action"] = RecommendedAction.RESPAWN.value
    tampered = receipt_from_dict(tampered_dict)
    result = verify_receipt(tampered, key.public_key())
    assert not result.ok
    assert any("recommended_action" in err for err in result.errors)


def test_assemble_rejects_cross_worktree_violation() -> None:
    """Assembly refuses a slice that leaks across worktrees."""
    audit_entries = [
        *_audit_slice(),
        {
            "event_type": "task.resolved",
            "session_id": "sess-1",
            "worktree_id": "wt-B",
        },
    ]
    with pytest.raises(CrossWorktreeFenceError):
        assemble_receipt(
            worker_id="abc123",
            worktree_id="wt-A",
            session_id="sess-1",
            stall_reason=StallReason.HEARTBEAT_STALE,
            audit_entries=audit_entries,
            identity=_identity(),
            prev_chain_digest="0" * 64,
        )


def test_canonical_bytes_are_byte_stable_under_dict_reorder() -> None:
    """Reordering dict keys in the JSON wire form yields the same canonical bytes."""
    key = _signing_key()
    receipt = assemble_receipt(
        worker_id="abc123",
        worktree_id="wt-A",
        session_id="sess-1",
        stall_reason=StallReason.HEARTBEAT_STALE,
        audit_entries=_audit_slice(),
        identity=_identity(),
        prev_chain_digest="0" * 64,
        respawn_budget_remaining=2,
    )
    signed = sign_receipt(receipt, signing_key=key)
    a = receipt_to_dict(signed)
    # Reorder keys in details + identity.
    b = json.loads(json.dumps(a))
    reordered = {k: a[k] for k in sorted(a.keys(), reverse=True)}
    reloaded = receipt_from_dict(reordered)
    assert canonical_receipt_bytes(reloaded) == canonical_receipt_bytes(receipt_from_dict(b))


def test_unsigned_receipt_fails_verification() -> None:
    """A receipt without a signature blob never verifies."""
    key = _signing_key()
    receipt = assemble_receipt(
        worker_id="abc123",
        worktree_id="wt-A",
        session_id="sess-1",
        stall_reason=StallReason.HEARTBEAT_STALE,
        audit_entries=_audit_slice(),
        identity=_identity(),
        prev_chain_digest="0" * 64,
    )
    result = verify_receipt(receipt, key.public_key())
    assert not result.ok
    assert any("unsigned" in err for err in result.errors)
