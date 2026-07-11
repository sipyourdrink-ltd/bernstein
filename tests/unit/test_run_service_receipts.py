"""Continuity proof + lifecycle-receipt unit tests for the detached run service (#2352).

The reattach artefact is a deterministic projection of the durable work
ledger: :func:`prove_continuity` walks the hash chain and proves the current
head is a forward extension of the head the operator last saw. Strip the
ledger chain and the proof is meaningless -- it is the audit chain in the
shape of ``attach``. These tests pin the determinism, the fork/off-record
refusal, and the offline receipt contract without spawning any process.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.persistence.work_ledger import (
    GENESIS_HASH,
    KIND_RUN_OPEN,
    KIND_TASK_COMPLETED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    LedgerReader,
    WorkLedger,
    run_ledger_dir,
)
from bernstein.core.run_service import (
    LIFECYCLE_TRANSITIONS,
    TRANSITION_COMPLETED,
    TRANSITION_DAEMON_RESTARTED,
    TRANSITION_DETACHED,
    TRANSITION_REATTACHED,
    TRANSITION_SUBMITTED,
    ContinuityProof,
    prove_continuity,
)


def _seed_ledger(sdd_dir: Path, run_id: str, *, tasks: int) -> WorkLedger:
    ledger = WorkLedger.open(run_ledger_dir(sdd_dir, run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    for i in range(tasks):
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id=f"t{i}")
    return ledger


def test_transition_vocabulary_is_stable() -> None:
    assert TRANSITION_SUBMITTED == "submitted"
    assert TRANSITION_DETACHED == "detached"
    assert TRANSITION_REATTACHED == "reattached"
    assert TRANSITION_DAEMON_RESTARTED == "daemon_restarted"
    assert TRANSITION_COMPLETED == "completed"
    # The five lifecycle transitions the issue enumerates.
    assert set(LIFECYCLE_TRANSITIONS) == {
        TRANSITION_SUBMITTED,
        TRANSITION_DETACHED,
        TRANSITION_REATTACHED,
        TRANSITION_DAEMON_RESTARTED,
        TRANSITION_COMPLETED,
    }


def test_continuity_ok_when_current_head_extends_boundary(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    ledger = _seed_ledger(sdd, "run-a", tasks=2)
    boundary = ledger.head_hash  # operator detaches here
    # Work advances off-terminal while the operator is away.
    ledger.append(kind=KIND_TASK_STARTED, task_id="t0")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t0")
    current = ledger.head_hash

    reader = LedgerReader(run_ledger_dir(sdd, "run-a"))
    proof = prove_continuity(reader, boundary, run_id="run-a")
    assert isinstance(proof, ContinuityProof)
    assert proof.ok
    assert proof.ledger_verified
    assert proof.boundary_head == boundary
    assert proof.current_head == current
    assert proof.entries_added == 2  # started + completed


def test_continuity_ok_from_genesis_on_first_attach(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _seed_ledger(sdd, "run-g", tasks=1)
    reader = LedgerReader(run_ledger_dir(sdd, "run-g"))
    proof = prove_continuity(reader, GENESIS_HASH, run_id="run-g")
    assert proof.ok
    assert proof.boundary_head == GENESIS_HASH
    assert proof.entries_added == reader.verify().entries


def test_continuity_refuses_unknown_boundary_head(tmp_path: Path) -> None:
    """A boundary head absent from the chain means off-record activity."""
    sdd = tmp_path / ".sdd"
    _seed_ledger(sdd, "run-f", tasks=1)
    reader = LedgerReader(run_ledger_dir(sdd, "run-f"))
    proof = prove_continuity(reader, "f" * 64, run_id="run-f")
    assert not proof.ok
    assert "boundary" in proof.reason.lower() or "not found" in proof.reason.lower()


def test_continuity_refuses_when_ledger_tampered(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    ledger = _seed_ledger(sdd, "run-t", tasks=2)
    boundary = ledger.head_hash
    ledger.append(kind=KIND_TASK_STARTED, task_id="t0")
    bucket = run_ledger_dir(sdd, "run-t") / "000000.jsonl"
    lines = bucket.read_text(encoding="utf-8").splitlines()
    # Corrupt an interior line's payload without fixing the hash.
    lines[1] = lines[1].replace(KIND_TASK_SCHEDULED, "task.tampered")
    bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reader = LedgerReader(run_ledger_dir(sdd, "run-t"))
    proof = prove_continuity(reader, boundary, run_id="run-t")
    assert not proof.ok
    assert not proof.ledger_verified


def test_continuity_proof_is_deterministic_across_cwd(tmp_path: Path, monkeypatch) -> None:
    """Two verifiers from different working dirs produce identical proof bytes."""
    sdd = tmp_path / ".sdd"
    ledger = _seed_ledger(sdd, "run-d", tasks=3)
    boundary = ledger.head_hash
    ledger.append(kind=KIND_TASK_STARTED, task_id="t0")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t0")

    reader = LedgerReader(run_ledger_dir(sdd, "run-d"))

    monkeypatch.chdir(tmp_path)
    first = prove_continuity(reader, boundary, run_id="run-d").to_canonical_json()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    second = prove_continuity(reader, boundary, run_id="run-d").to_canonical_json()
    assert first == second
