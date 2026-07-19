"""Chain-anchored runtime context capsule (#2545, AC3 + AC4).

The capsule is a content-addressed projection of what a worker was given. Its
hash lands in the spawn record, the run journal, and the audit chain; a verifier
holding only the journal and the chain recomputes it byte-identically. A context
divergence surfaces as a hash mismatch, and a mock-layer capsule never verifies
as real.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bernstein.core.agents.context_capsule import (
    CAPSULE_RECORDED_EVENT,
    ContextCapsule,
    build_context_capsule,
    capsule_spawn_binding,
    read_capsule_record,
    seal_and_bind,
    seal_context_capsule,
    seal_mock_capsule,
    verify_context_capsule,
    verify_signature,
    write_capsule_record,
)
from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit_chain import EVENT_CONTEXT_CAPSULE, AuditChainStore


def _chain(sdd: Path) -> AuditChainStore:
    return AuditChainStore(sdd / "audit", key=b"k" * 32)


def _capsule(**over: object) -> ContextCapsule:
    base: dict[str, object] = dict(
        task_id="task-1",
        run_id="run-1",
        params_hash="sha256:" + "p" * 64,
        worktree_path="/wt/task-1",
        role="backend",
        budget_remaining_tokens=50_000,
        budget_remaining_usd_micros=1_250_000,
        dependency_state={"task-0": "done"},
        audit_chain_head="head-abc",
        intent_capsule_hash="sha256:" + "i" * 64,
        spawned_at=1_700_000_000,
    )
    base.update(over)
    return build_context_capsule(**base)  # type: ignore[arg-type]


def _seal(sdd: Path, capsule: ContextCapsule) -> None:
    priv, pub = generate_keypair()
    chain = _chain(sdd)
    journal = EventJournal("run-1", sdd)
    seal_and_bind(
        chain=chain,
        sdd_dir=sdd,
        journal=journal,
        capsule=capsule,
        private_key_pem=priv,
        public_key_pem=pub,
    )


# ---------------------------------------------------------------------------
# AC3: input attestation -- same params_hash on all three surfaces
# ---------------------------------------------------------------------------


def test_spawn_record_journal_capsule_share_params_hash(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    priv, pub = generate_keypair()
    chain = _chain(sdd)
    journal = EventJournal("run-1", sdd)
    capsule = _capsule()
    _signed, binding = seal_and_bind(
        chain=chain,
        sdd_dir=sdd,
        journal=journal,
        capsule=capsule,
        private_key_pem=priv,
        public_key_pem=pub,
    )
    ph = capsule.params_hash
    # spawn record
    assert binding["context_params_hash"] == ph
    assert binding["context_capsule_hash"] == capsule.capsule_hash()
    # chain
    chain_event = chain.query(event_type=EVENT_CONTEXT_CAPSULE)[-1]
    assert chain_event.details["params_hash"] == ph
    assert chain_event.details["capsule_hash"] == capsule.capsule_hash()
    # journal
    from bernstein.core.replay.journal import load_events

    events = load_events(journal.path)
    recorded = [e for e in events if e.get("event") == CAPSULE_RECORDED_EVENT]
    assert recorded and recorded[-1]["capsule_hash"] == capsule.capsule_hash()


def test_capsule_binding_carries_params_hash() -> None:
    binding = capsule_spawn_binding(task_id="t", params_hash="sha256:pp", capsule_hash="sha256:cc")
    assert binding["context_params_hash"] == "sha256:pp"
    assert binding["context_capsule_hash"] == "sha256:cc"


# ---------------------------------------------------------------------------
# AC4: capsule verification -- real succeeds, mock fails with mock diagnostic
# ---------------------------------------------------------------------------


def test_real_capsule_verifies_offline(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    capsule = _capsule()
    _seal(sdd, capsule)
    # A fresh chain (only the journal + chain + capsule record on disk).
    result = verify_context_capsule(sdd_dir=sdd, chain=_chain(sdd), task_id="task-1")
    assert result.ok, result.reason
    assert result.signature_ok and result.chain_ok and result.journal_ok
    assert not result.is_mock


def test_mock_capsule_fails_with_mock_diagnostic(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    priv, pub = generate_keypair()
    capsule = _capsule()
    mock_signed = seal_mock_capsule(capsule, priv, pub)
    write_capsule_record(sdd, mock_signed)
    result = verify_context_capsule(sdd_dir=sdd, chain=_chain(sdd), task_id="task-1")
    assert result.ok is False
    assert result.is_mock
    assert "mock" in result.reason.lower()


def test_mock_signature_never_verifies_as_real() -> None:
    priv, pub = generate_keypair()
    capsule = _capsule()
    real = seal_context_capsule(capsule, priv, pub)
    mock = seal_mock_capsule(capsule, priv, pub)
    assert verify_signature(real) == (True, False)
    assert verify_signature(mock) == (False, True)
    # Even forging the on-disk flag cannot make the mock verify as real.
    forged = replace(mock, is_mock=False)
    real_ok, _ = verify_signature(forged)
    assert real_ok is False


def test_worker_quoted_hash_checkable_offline(tmp_path: Path) -> None:
    # A capsule hash a worker quotes in its completion payload is checkable by a
    # verifier holding only the journal and the chain.
    sdd = tmp_path / ".sdd"
    capsule = _capsule()
    _seal(sdd, capsule)
    quoted = capsule.capsule_hash()
    signed = read_capsule_record(sdd, "task-1")
    assert signed is not None
    assert signed.capsule.capsule_hash() == quoted
    assert verify_context_capsule(sdd_dir=sdd, chain=_chain(sdd), task_id="task-1").ok


# ---------------------------------------------------------------------------
# AC3: divergence detected as hash mismatch
# ---------------------------------------------------------------------------


def test_context_divergence_detected_as_hash_mismatch(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    capsule = _capsule()
    _seal(sdd, capsule)
    # Tamper the on-disk capsule (different budget) but keep the old signature:
    # its recomputed hash no longer matches the chain / journal record.
    signed = read_capsule_record(sdd, "task-1")
    assert signed is not None
    diverged = replace(signed, capsule=replace(signed.capsule, budget_remaining_tokens=999))
    write_capsule_record(sdd, diverged)
    result = verify_context_capsule(sdd_dir=sdd, chain=_chain(sdd), task_id="task-1")
    assert result.ok is False
    assert "signature" in result.reason.lower() or "not anchored" in result.reason.lower()


def test_tampered_chain_entry_flips_verification(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    capsule = _capsule()
    _seal(sdd, capsule)
    log_files = list((sdd / "audit").glob("*.jsonl"))
    assert log_files
    content = log_files[0].read_text().replace('"backend"', '"frontend"')
    # Only rewrite if the role appeared in the chain payload; otherwise corrupt
    # a stable field the context event always carries.
    content = content.replace(capsule.params_hash, "sha256:" + "z" * 64)
    log_files[0].write_text(content)
    result = verify_context_capsule(sdd_dir=sdd, chain=AuditChainStore(sdd / "audit", key=b"k" * 32), task_id="task-1")
    assert result.ok is False


def test_missing_capsule_reports_absent(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    (sdd / "audit").mkdir(parents=True, exist_ok=True)
    result = verify_context_capsule(sdd_dir=sdd, chain=_chain(sdd), task_id="nope")
    assert result.ok is False
    assert "no context capsule" in result.reason


def test_capsule_hash_is_deterministic() -> None:
    a = _capsule()
    b = _capsule()
    assert a.capsule_hash() == b.capsule_hash()
    c = _capsule(params_hash="sha256:" + "q" * 64)
    assert c.capsule_hash() != a.capsule_hash()


def test_capsule_hash_independent_of_dependency_order() -> None:
    a = build_context_capsule(task_id="t", run_id="r", dependency_state={"x": "done", "y": "pending"})
    b = build_context_capsule(task_id="t", run_id="r", dependency_state={"y": "pending", "x": "done"})
    assert a.capsule_hash() == b.capsule_hash()


def test_from_dict_round_trip() -> None:
    a = _capsule()
    b = ContextCapsule.from_dict(a.to_dict())
    assert a == b


def test_verify_rejects_when_journal_missing(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    priv, pub = generate_keypair()
    chain = _chain(sdd)
    capsule = _capsule()
    signed = seal_context_capsule(capsule, priv, pub)
    write_capsule_record(sdd, signed)
    from bernstein.core.agents.context_capsule import anchor_capsule

    anchor_capsule(chain, signed)
    # No journal recorded -> verify fails on the missing journal.
    result = verify_context_capsule(sdd_dir=sdd, chain=_chain(sdd), task_id="task-1")
    assert result.ok is False
    assert "journal" in result.reason.lower()


@pytest.mark.parametrize("field_name", ["params_hash", "audit_chain_head", "role", "worktree_path"])
def test_any_field_change_changes_hash(field_name: str) -> None:
    base = _capsule()
    changed = replace(base, **{field_name: "CHANGED"})
    assert base.capsule_hash() != changed.capsule_hash()


def test_verify_refuses_a_run_journal_planted_outside_the_runs_root(tmp_path: Path) -> None:
    """The capsule verifier must not re-derive from a journal outside the tree.

    Pins the routing of ``verify_context_capsule`` through the shared
    containment barrier. The planted journal's Merkle chain is intact - it
    is written by the real writer - so ``verify_journal`` cannot tell it
    apart from ours; only containment can. Reverting the routing to a raw
    join makes this verify a planted journal and fails the test.
    """
    import pytest

    sdd = tmp_path / ".sdd"
    capsule = _capsule()
    _seal(sdd, capsule)

    run_dir = sdd / "runs" / "run-1"
    outside = tmp_path / "outside_runs"
    run_dir.rename(outside)
    try:
        run_dir.symlink_to(outside, target_is_directory=True)
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("cannot create symlinks on this platform")

    result = verify_context_capsule(sdd_dir=sdd, chain=_chain(sdd), task_id="task-1")

    # Unrouted, this journal reads cleanly and the capsule verifies ok.
    assert not result.ok
    assert "invalid run id" in result.reason
