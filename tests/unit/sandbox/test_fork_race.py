"""Acceptance + unit tests for the deterministic fork-and-race (#2613).

The headline test is the issue's empirical acceptance gate: run the same
fork-race twice over one content-addressed base snapshot with the same
candidate set, and the two signed receipts must be byte-identical and both
verify under the Ed25519 public key. Then a stored *loser* snapshot blob is
mutated and the CAS re-hash the verifier performs must fail - proving the
race result is meaningless once the content-addressing / signing substrate
is removed.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.orchestration.best_of_n import CandidateResult
from bernstein.core.persistence.cas_store import CASIntegrityError, CASStore
from bernstein.core.sandbox.backends._vmmonitor import FakeMonitor
from bernstein.core.sandbox.backends.microvm import MicroVMSandboxBackend
from bernstein.core.sandbox.fork_race import (
    FORK_RACE_EVENT_TYPE,
    fork_race,
)
from bernstein.core.sandbox.manifest import FileEntry, WorkspaceManifest
from bernstein.core.sandbox.selection_receipt import (
    canonical_receipt_bytes,
    receipt_to_dict,
    snapshot_digests,
    verify_receipt,
)
from bernstein.core.security.audit import AuditLog

if TYPE_CHECKING:
    from pathlib import Path

_AUDIT_KEY = b"unit-test-audit-key-not-a-secret"


def _backend(tmp_path: Path) -> MicroVMSandboxBackend:
    return MicroVMSandboxBackend(
        monitor_factory=lambda root: FakeMonitor(root=root),
        cas=CASStore(tmp_path / "cas"),
    )


async def _base_snapshot(backend: MicroVMSandboxBackend) -> str:
    manifest = WorkspaceManifest(root="/workspace", files=(FileEntry(path="base.txt", content=b"BASE"),))
    session = await backend.create(manifest)
    digest = await session.snapshot()
    await backend.destroy(session)
    return digest


async def _run_candidate(session: object, index: int) -> CandidateResult:
    """Deterministic per-candidate work: a fixed file + index-derived scores."""
    await session.write(f"cand{index}.txt", f"work-{index}".encode())  # type: ignore[attr-defined]
    return CandidateResult(
        task_id=f"candidate-{index}",
        tests_passing=(index % 2 == 0),
        lint_score=max(0.0, 1.0 - 0.1 * index),
    )


@pytest.mark.asyncio
async def test_fork_race_is_deterministic_and_tamper_evident(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    key = Ed25519PrivateKey.generate()
    base = await _base_snapshot(backend)

    r1 = await fork_race(backend=backend, base_snapshot_digest=base, run_candidate=_run_candidate, k=3, signing_key=key)
    r2 = await fork_race(backend=backend, base_snapshot_digest=base, run_candidate=_run_candidate, k=3, signing_key=key)

    # (a) Same race twice -> byte-identical signed receipts, both verifying.
    assert canonical_receipt_bytes(r1) == canonical_receipt_bytes(r2)
    assert r1.signature_b64 == r2.signature_b64
    assert receipt_to_dict(r1) == receipt_to_dict(r2)
    assert verify_receipt(r1).ok
    assert verify_receipt(r2).ok

    # (b) Mutate a LOSER blob -> the CAS re-hash (base + all candidates) fails,
    # proving the verifier does not only check the winner.
    cas = backend.cas
    loser_digest = r1.loser_snapshot_digests[0]
    blob = cas.root / loser_digest[:2] / loser_digest
    corrupt = bytearray(blob.read_bytes())
    corrupt[7] ^= 0xFF
    blob.write_bytes(bytes(corrupt))

    with pytest.raises(CASIntegrityError):
        for digest in snapshot_digests(r1):
            cas.get(digest, verify=True)


@pytest.mark.asyncio
async def test_winner_selection_has_no_llm_and_is_stable(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    key = Ed25519PrivateKey.generate()
    base = await _base_snapshot(backend)
    receipts = [
        await fork_race(backend=backend, base_snapshot_digest=base, run_candidate=_run_candidate, k=4, signing_key=key)
        for _ in range(3)
    ]
    winners = {r.winner_task_id for r in receipts}
    assert len(winners) == 1  # deterministic winner across repeated races


@pytest.mark.asyncio
async def test_fork_race_appends_exactly_one_audit_entry(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    key = Ed25519PrivateKey.generate()
    base = await _base_snapshot(backend)
    audit_dir = tmp_path / "audit"
    audit_log = AuditLog(audit_dir, key=_AUDIT_KEY)

    await fork_race(
        backend=backend,
        base_snapshot_digest=base,
        run_candidate=_run_candidate,
        k=3,
        signing_key=key,
        audit_log=audit_log,
    )

    entries = [
        json.loads(line) for path in audit_dir.glob("*.jsonl") for line in path.read_text().splitlines() if line.strip()
    ]
    fork_entries = [e for e in entries if e.get("event_type") == FORK_RACE_EVENT_TYPE]
    assert len(fork_entries) == 1
    # The chain still verifies after the append.
    ok, errors = audit_log.verify()
    assert ok, errors


@pytest.mark.asyncio
async def test_candidate_failure_is_not_masked_by_destroy_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a candidate fails AND its session teardown also fails, the ORIGINAL
    candidate exception must propagate - not the cleanup error."""
    backend = _backend(tmp_path)
    key = Ed25519PrivateKey.generate()
    base = await _base_snapshot(backend)

    async def boom_candidate(session: object, index: int) -> CandidateResult:
        raise ValueError(f"candidate {index} boom")

    async def boom_destroy(session: object) -> None:
        raise RuntimeError("destroy boom")

    monkeypatch.setattr(backend, "destroy", boom_destroy)

    with pytest.raises(ValueError, match="candidate .* boom"):
        await fork_race(
            backend=backend,
            base_snapshot_digest=base,
            run_candidate=boom_candidate,
            k=2,
            signing_key=key,
        )


@pytest.mark.asyncio
async def test_concurrent_fork_races_do_not_fork_audit_chain(tmp_path: Path) -> None:
    """Concurrent fork_race() calls sharing one AuditLog with no lock path must
    still serialise their (now off-loop, threaded) appends - the HMAC chain must
    remain single and verifiable, not forked on a shared prev_hmac."""
    key = Ed25519PrivateKey.generate()
    audit_dir = tmp_path / "audit"
    audit_log = AuditLog(audit_dir, key=_AUDIT_KEY)
    n = 4

    async def one_race(i: int) -> None:
        backend = _backend(tmp_path / f"race{i}")
        base = await _base_snapshot(backend)
        await fork_race(
            backend=backend,
            base_snapshot_digest=base,
            run_candidate=_run_candidate,
            k=3,
            signing_key=key,
            audit_log=audit_log,  # shared; audit_lock_path deliberately None
        )

    await asyncio.gather(*(one_race(i) for i in range(n)))

    entries = [
        json.loads(line) for path in audit_dir.glob("*.jsonl") for line in path.read_text().splitlines() if line.strip()
    ]
    fork_entries = [e for e in entries if e.get("event_type") == FORK_RACE_EVENT_TYPE]
    assert len(fork_entries) == n  # every append landed, none lost to a race
    ok, errors = audit_log.verify()
    assert ok, errors  # chain not forked


@pytest.mark.asyncio
async def test_fork_race_rejects_duplicate_task_ids(tmp_path: Path) -> None:
    """Candidates that return a colliding task_id must be rejected, not silently
    collapsed into one entry in the signed receipt."""
    backend = _backend(tmp_path)
    key = Ed25519PrivateKey.generate()
    base = await _base_snapshot(backend)

    async def dup_candidate(session: object, index: int) -> CandidateResult:
        await session.write(f"c{index}.txt", f"work-{index}".encode())  # type: ignore[attr-defined]
        return CandidateResult(task_id="same-id", tests_passing=True)

    with pytest.raises(ValueError, match="unique"):
        await fork_race(
            backend=backend,
            base_snapshot_digest=base,
            run_candidate=dup_candidate,
            k=2,
            signing_key=key,
        )


@pytest.mark.asyncio
async def test_fork_race_rejects_zero_k(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    key = Ed25519PrivateKey.generate()
    base = await _base_snapshot(backend)
    with pytest.raises(ValueError, match="k >= 1"):
        await fork_race(backend=backend, base_snapshot_digest=base, run_candidate=_run_candidate, k=0, signing_key=key)


@pytest.mark.asyncio
async def test_fork_race_audit_lock_path_is_honoured(tmp_path: Path) -> None:
    """Passing a lock path serialises the append and leaves the chain verifiable."""
    backend = _backend(tmp_path)
    key = Ed25519PrivateKey.generate()
    base = await _base_snapshot(backend)
    audit_dir = tmp_path / "audit"
    audit_log = AuditLog(audit_dir, key=_AUDIT_KEY)
    lock_path = audit_dir / ".fork_race.lock"

    await fork_race(
        backend=backend,
        base_snapshot_digest=base,
        run_candidate=_run_candidate,
        k=3,
        signing_key=key,
        audit_log=audit_log,
        audit_lock_path=lock_path,
    )

    ok, errors = audit_log.verify()
    assert ok, errors
    assert lock_path.exists()  # the lock file was created and released cleanly
