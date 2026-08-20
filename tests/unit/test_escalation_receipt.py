"""Unit tests for journal-anchored stall escalation receipts (#2299).

The receipt fixes the exact failure window: on a stall it captures the last
``N`` journal entries by their Merkle ``event_hash``, references a valid f03
fork point for resume, recommends a deterministic action, signs the binding
with the install Ed25519 identity, and anchors the canonical bytes in the
escalation lineage spine. ``verify`` reconstructs the same window from the
journal and confirms every entry hash matches -- a tampered journal entry
inside the window breaks verification.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.orchestration.escalation import (
    DEFAULT_ESCALATION_WINDOW,
    EscalationReceipt,
    assemble_escalation_receipt,
    load_or_create_escalation_identity,
    read_escalation_receipt,
    verify_escalation_receipt,
)
from bernstein.core.orchestration.supervisor_receipt import (
    RecommendedAction,
    StallReason,
)
from bernstein.core.replay.journal import EventJournal, load_events

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_HMAC_KEY = b"k" * 32
_RUN_ID = "run-stall-1"
_WORKER_ID = "abcdef012345"
_SESSION_ID = "sess-1"


def _sdd(tmp_path: Path) -> Path:
    return tmp_path / ".sdd"


def _lineage_root(tmp_path: Path) -> Path:
    return _sdd(tmp_path) / "lineage"


def _identity_dir(tmp_path: Path) -> Path:
    return _sdd(tmp_path) / "identity"


def _build_journal(tmp_path: Path, *, n_events: int = 24, with_snapshot_at: int | None = 3) -> EventJournal:
    """Build a run journal with ``n_events`` rows and an optional snapshot.

    The snapshot event carries a fake sha; the fork ref is created separately
    by the tests that exercise the fork-reference AC so this stays cheap.
    """
    journal = EventJournal(_RUN_ID, _sdd(tmp_path))
    for i in range(n_events):
        if with_snapshot_at is not None and i == with_snapshot_at:
            journal.record("snapshot", snapshot_sha="d" * 40, step_index=i)
        else:
            journal.record("task.tick", session_id=_SESSION_ID, seq=i)
    return journal


def _assemble(
    tmp_path: Path,
    *,
    stall_reason: StallReason = StallReason.HEARTBEAT_STALE,
    window: int = DEFAULT_ESCALATION_WINDOW,
    respawn_budget_remaining: int = 2,
    fork_step: int | None = 3,
) -> EscalationReceipt:
    _build_journal(tmp_path)
    private_pem, public_pem = load_or_create_escalation_identity(_identity_dir(tmp_path))
    return assemble_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        run_id=_RUN_ID,
        worker_id=_WORKER_ID,
        session_id=_SESSION_ID,
        worktree_id="wt-1",
        stall_reason=stall_reason,
        respawn_budget_remaining=respawn_budget_remaining,
        fork_step=fork_step,
        window=window,
        install_rev="abc1234567890def",
        timestamp=1_700_000_000,
    )


# ---------------------------------------------------------------------------
# AC1 - a stall produces a signed receipt anchored to the journal
# ---------------------------------------------------------------------------


def test_ac1_receipt_is_signed_and_journal_anchored(tmp_path: Path) -> None:
    receipt = _assemble(tmp_path)
    assert receipt.signature
    assert receipt.signer_public_key_pem
    assert receipt.journal_entry_hash  # spine anchor
    # The journal head at stall is captured and equals the live journal head.
    journal = EventJournal(_RUN_ID, _sdd(tmp_path))
    # Recompute the head over the on-disk journal.
    events = load_events(journal.path).events
    assert receipt.journal_head_at_stall == events[-1]["event_hash"]
    assert receipt.window_entry_hashes  # non-empty window
    del journal


def test_ac1_receipt_persisted_and_reloads(tmp_path: Path) -> None:
    receipt = _assemble(tmp_path)
    loaded = read_escalation_receipt(_sdd(tmp_path), receipt.receipt_id)
    assert loaded is not None
    assert loaded.to_dict() == receipt.to_dict()


# ---------------------------------------------------------------------------
# AC2 - verify reconstructs the exact last-N window from the journal
# ---------------------------------------------------------------------------


def test_ac2_verify_reconstructs_window(tmp_path: Path) -> None:
    receipt = _assemble(tmp_path, window=8)
    # The window binds exactly the trailing 8 event hashes of the journal.
    events = load_events(EventJournal(_RUN_ID, _sdd(tmp_path)).path).events
    expected = [e["event_hash"] for e in events[-8:]]
    assert list(receipt.window_entry_hashes) == expected

    result = verify_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        receipt_id=receipt.receipt_id,
    )
    assert result.ok, result.reason


def test_ac2_window_capped_to_journal_length(tmp_path: Path) -> None:
    # A window larger than the journal captures the whole journal, no crash.
    receipt = _assemble(tmp_path, window=1000)
    events = load_events(EventJournal(_RUN_ID, _sdd(tmp_path)).path).events
    assert len(receipt.window_entry_hashes) == len(events)


# ---------------------------------------------------------------------------
# AC3 - recommended_action is deterministic across two runs
# ---------------------------------------------------------------------------


def test_ac3_recommended_action_deterministic(tmp_path: Path) -> None:
    a = _assemble(tmp_path / "a")
    b = _assemble(tmp_path / "b")
    assert a.recommended_action == b.recommended_action
    # HEARTBEAT_STALE with budget and a clean slice -> RESPAWN.
    assert a.recommended_action == RecommendedAction.RESPAWN


def test_ac3_recommended_action_park_when_exhausted(tmp_path: Path) -> None:
    receipt = _assemble(tmp_path, stall_reason=StallReason.RESPAWN_EXHAUSTED)
    assert receipt.recommended_action == RecommendedAction.PARK


# ---------------------------------------------------------------------------
# AC4 - the receipt references a valid f03 fork point for resume
# ---------------------------------------------------------------------------


def test_ac4_fork_ref_points_at_snapshot_step(tmp_path: Path) -> None:
    receipt = _assemble(tmp_path, fork_step=3)
    assert receipt.fork_ref is not None
    assert receipt.fork_ref.fork_step == 3
    assert receipt.fork_ref.snapshot_sha == "d" * 40
    assert receipt.fork_ref.run_id == _RUN_ID


def test_ac4_fork_step_without_snapshot_is_refused(tmp_path: Path) -> None:
    # Asking to fork at a step with no snapshot event refuses at assembly.
    from bernstein.core.orchestration.escalation import EscalationError

    with pytest.raises(EscalationError, match="no snapshot"):
        _assemble(tmp_path, fork_step=7)


def test_ac4_no_fork_step_yields_no_fork_ref(tmp_path: Path) -> None:
    receipt = _assemble(tmp_path, fork_step=None)
    assert receipt.fork_ref is None
    result = verify_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        receipt_id=receipt.receipt_id,
    )
    assert result.ok, result.reason


# ---------------------------------------------------------------------------
# AC5 - tampering with a journal entry in the window breaks verification
# ---------------------------------------------------------------------------


def test_ac5_tampered_window_entry_fails_verify(tmp_path: Path) -> None:
    receipt = _assemble(tmp_path, window=8)
    journal_path = _sdd(tmp_path) / "runs" / _RUN_ID / "journal.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    # Tamper the payload of the last row (inside the window) without fixing
    # its event_hash: the reconstructed window hash diverges.
    import json as _json

    last = _json.loads(lines[-1])
    last["seq"] = 9999
    lines[-1] = _json.dumps(last)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        receipt_id=receipt.receipt_id,
    )
    assert not result.ok
    assert "journal" in result.reason.lower() or "window" in result.reason.lower()


def test_ac5_tampered_receipt_binding_fails_verify(tmp_path: Path) -> None:
    receipt = _assemble(tmp_path)
    # Flip the recommended action on disk; the signature no longer covers it.
    import json as _json

    path = _sdd(tmp_path).joinpath("escalation", "receipts", f"{receipt.receipt_id}.json")
    row = _json.loads(path.read_text(encoding="utf-8"))
    row["recommended_action"] = "park"
    path.write_text(_json.dumps(row), encoding="utf-8")

    result = verify_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        receipt_id=receipt.receipt_id,
    )
    assert not result.ok


# ---------------------------------------------------------------------------
# Audit-chain mirror
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Projection for the TUI / web supervisor
# ---------------------------------------------------------------------------


def test_projection_is_compact_and_safe(tmp_path: Path) -> None:
    from bernstein.core.orchestration.escalation import project_escalation_receipt

    receipt = _assemble(tmp_path)
    view = project_escalation_receipt(receipt)
    # The projection surfaces the operator-relevant fields...
    assert view["worker_id"] == receipt.worker_id
    assert view["stall_reason"] == receipt.stall_reason.value
    assert view["recommended_action"] == receipt.recommended_action.value
    assert view["window_size"] == len(receipt.window_entry_hashes)
    assert view["journal_entry_hash"] == receipt.journal_entry_hash
    # ...but never leaks the private signing material or the raw window hashes.
    assert "signature" not in view
    assert "window_entry_hashes" not in view


def test_audit_chain_mirror_records_escalation(tmp_path: Path) -> None:
    from bernstein.core.security.audit_chain import (
        EVENT_ESCALATION_RECEIPT,
        AuditChainStore,
        record_escalation_receipt,
    )

    receipt = _assemble(tmp_path)
    chain = AuditChainStore(_sdd(tmp_path) / "audit", key=_HMAC_KEY)
    event = record_escalation_receipt(
        chain=chain,
        run_id=receipt.run_id,
        worker_id=receipt.worker_id,
        session_id=receipt.session_id,
        stall_reason=receipt.stall_reason.value,
        recommended_action=receipt.recommended_action.value,
        journal_head_at_stall=receipt.journal_head_at_stall,
        window_size=len(receipt.window_entry_hashes),
        fork_snapshot_sha=receipt.fork_ref.snapshot_sha if receipt.fork_ref else "",
        journal_entry_hash=receipt.journal_entry_hash,
    )
    assert event.event_type == EVENT_ESCALATION_RECEIPT
    assert "prev_chain_digest" in event.details
    assert event.details["run_id"] == receipt.run_id
    # The session link is read from the record, not reconstructed by matching.
    assert event.details["session_id"] == receipt.session_id
    ok, errors = chain.verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# Degraded terminal receipts on missing or empty journal (#3737)
# ---------------------------------------------------------------------------


def _assemble_missing_journal(
    tmp_path: Path,
    *,
    stall_reason: StallReason = StallReason.HEARTBEAT_STALE,
    window: int = DEFAULT_ESCALATION_WINDOW,
    respawn_budget_remaining: int = 2,
    fork_step: int | None = None,
) -> EscalationReceipt:
    private_pem, public_pem = load_or_create_escalation_identity(_identity_dir(tmp_path))
    return assemble_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        run_id=_RUN_ID,
        worker_id=_WORKER_ID,
        session_id=_SESSION_ID,
        worktree_id="wt-1",
        stall_reason=stall_reason,
        respawn_budget_remaining=respawn_budget_remaining,
        fork_step=fork_step,
        window=window,
        install_rev="abc1234567890def",
        timestamp=1_700_000_000,
    )


def _assemble_empty_journal(
    tmp_path: Path,
    *,
    stall_reason: StallReason = StallReason.HEARTBEAT_STALE,
    window: int = DEFAULT_ESCALATION_WINDOW,
    respawn_budget_remaining: int = 2,
    fork_step: int | None = None,
) -> EscalationReceipt:
    journal_path = _sdd(tmp_path) / "runs" / _RUN_ID / "journal.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.touch()
    private_pem, public_pem = load_or_create_escalation_identity(_identity_dir(tmp_path))
    return assemble_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        run_id=_RUN_ID,
        worker_id=_WORKER_ID,
        session_id=_SESSION_ID,
        worktree_id="wt-1",
        stall_reason=stall_reason,
        respawn_budget_remaining=respawn_budget_remaining,
        fork_step=fork_step,
        window=window,
        install_rev="abc1234567890def",
        timestamp=1_700_000_000,
    )


def test_missing_journal_produces_degraded_terminal_receipt(tmp_path: Path) -> None:
    receipt = _assemble_missing_journal(tmp_path)
    assert receipt.journal_state == "missing"
    assert receipt.run_id == _RUN_ID
    assert receipt.timestamp == 1_700_000_000
    assert receipt.recommended_action == RecommendedAction.RESPAWN
    assert receipt.window_entry_hashes == ()
    assert receipt.journal_head_at_stall == ""
    assert receipt.signature
    assert receipt.signer_public_key_pem
    assert receipt.journal_entry_hash

    loaded = read_escalation_receipt(_sdd(tmp_path), receipt.receipt_id)
    assert loaded is not None
    assert loaded.journal_state == "missing"
    assert loaded.to_dict() == receipt.to_dict()

    result = verify_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        receipt_id=receipt.receipt_id,
    )
    assert result.ok, result.reason


def test_empty_journal_produces_degraded_terminal_receipt(tmp_path: Path) -> None:
    receipt = _assemble_empty_journal(tmp_path)
    assert receipt.journal_state == "empty"
    assert receipt.run_id == _RUN_ID
    assert receipt.timestamp == 1_700_000_000
    assert receipt.recommended_action == RecommendedAction.RESPAWN
    assert receipt.window_entry_hashes == ()
    assert receipt.journal_head_at_stall == ""
    assert receipt.signature
    assert receipt.signer_public_key_pem
    assert receipt.journal_entry_hash

    loaded = read_escalation_receipt(_sdd(tmp_path), receipt.receipt_id)
    assert loaded is not None
    assert loaded.journal_state == "empty"
    assert loaded.to_dict() == receipt.to_dict()

    result = verify_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        receipt_id=receipt.receipt_id,
    )
    assert result.ok, result.reason


def test_degraded_receipt_verify_names_the_degradation(tmp_path: Path) -> None:
    """A degraded receipt verifies, but never reports as a reconstructed window.

    ``ok`` alone cannot be the whole answer: a caller that only reads ``ok``
    would print "failure window reconstructs from the journal" over a receipt
    that never had a window. The reason names the journal state instead.
    """
    receipt = _assemble_missing_journal(tmp_path)
    result = verify_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        receipt_id=receipt.receipt_id,
    )
    assert result.ok
    assert "missing" in result.reason
    assert "degraded" in result.reason


def test_degraded_empty_receipt_verify_names_the_degradation(tmp_path: Path) -> None:
    receipt = _assemble_empty_journal(tmp_path)
    result = verify_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        receipt_id=receipt.receipt_id,
    )
    assert result.ok
    assert "empty" in result.reason
    assert "degraded" in result.reason


def test_missing_journal_receipt_fails_when_the_journal_is_there_after_all(tmp_path: Path) -> None:
    """A recorded absence stays falsifiable.

    ``journal_state='missing'`` skips the window reconstruction every other
    receipt is held to, so the claim itself has to be checked: a journal that
    holds entries for the run contradicts the receipt, and verification must
    say so instead of returning clean.
    """
    receipt = _assemble_missing_journal(tmp_path)
    journal = _build_journal(tmp_path, n_events=5, with_snapshot_at=None)
    del journal
    assert load_events(_sdd(tmp_path) / "runs" / _RUN_ID / "journal.jsonl").events

    result = verify_escalation_receipt(
        sdd_dir=_sdd(tmp_path),
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_HMAC_KEY,
        receipt_id=receipt.receipt_id,
    )
    assert not result.ok
    assert "contradicted by the store" in result.reason
    assert _RUN_ID in result.reason
    assert "5 entries" in result.reason
