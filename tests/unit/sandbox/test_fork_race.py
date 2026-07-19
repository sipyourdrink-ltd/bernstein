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
async def test_fork_race_selection_is_byte_identical_across_varied_axes(tmp_path: Path) -> None:
    """Determinism gate that actually varies the axes it is named for.

    Across two runs the candidate *set* (task_id -> deliverable) is held fixed
    while three axes vary: candidate completion order (opposite per-index
    sleeps), the wall-clock (``runtime_s`` differs by an order of magnitude),
    and the order results are assembled into the receipt's mappings (the
    task-id-to-slot assignment is reversed). The signed selection must be
    byte-identical regardless. A regression that ranked in completion order, or
    leaked a wall-clock axis into the signed body, diverges here (the old test
    left the wall-clock axis inert, so it could not detect either).
    """
    key = Ed25519PrivateKey.generate()
    k = 4

    def deliverable(slot: int) -> tuple[bool, float]:
        return (slot % 2 == 0), max(0.0, 1.0 - 0.1 * slot)

    async def make_receipt(*, reverse: bool, runtimes: list[float]) -> object:
        backend = _backend(tmp_path / ("rev" if reverse else "fwd"))
        base = await _base_snapshot(backend)

        async def run_candidate(session: object, index: int) -> CandidateResult:
            # Reversed assignment varies which submission slot produces which
            # task_id (mapping-assembly order); the opposite per-index sleep
            # varies completion order; runtime_s varies the wall-clock axis.
            slot = (k - 1 - index) if reverse else index
            await session.write(f"c{slot}.txt", f"work-{slot}".encode())  # type: ignore[attr-defined]
            await asyncio.sleep(0.001 * (index if reverse else (k - 1 - index)))
            tests_passing, lint = deliverable(slot)
            return CandidateResult(
                task_id=f"candidate-{slot}",
                tests_passing=tests_passing,
                lint_score=lint,
                runtime_s=runtimes[index],
            )

        return await fork_race(
            backend=backend,
            base_snapshot_digest=base,
            run_candidate=run_candidate,
            k=k,
            signing_key=key,
        )

    r1 = await make_receipt(reverse=False, runtimes=[0.10, 0.20, 0.30, 0.40])
    r2 = await make_receipt(reverse=True, runtimes=[9.9, 8.8, 7.7, 6.6])

    assert canonical_receipt_bytes(r1) == canonical_receipt_bytes(r2)
    assert r1.signature_b64 == r2.signature_b64
    assert r1.winner_task_id == r2.winner_task_id
    assert verify_receipt(r1).ok
    assert verify_receipt(r2).ok


@pytest.mark.asyncio
async def test_candidate_failure_not_masked_by_base_exception_in_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A teardown that raises a *BaseException* must not replace the original.

    The old cleanup caught only ``Exception``, so a ``BaseException`` from
    ``destroy`` (a cancellation injected into it, say) propagated *past* the
    ``raise`` that re-surfaces the candidate failure - masking it. The cleanup
    must catch ``BaseException`` around the reap and re-raise the original
    candidate exception verbatim.
    """
    backend = _backend(tmp_path)
    key = Ed25519PrivateKey.generate()
    base = await _base_snapshot(backend)

    class _BaseBoom(BaseException):
        pass

    async def boom_candidate(session: object, index: int) -> CandidateResult:
        raise ValueError(f"candidate {index} boom")

    async def base_boom_destroy(session: object) -> None:
        raise _BaseBoom("teardown base-exception boom")

    monkeypatch.setattr(backend, "destroy", base_boom_destroy)

    with pytest.raises(ValueError, match="candidate .* boom"):
        await fork_race(
            backend=backend,
            base_snapshot_digest=base,
            run_candidate=boom_candidate,
            k=2,
            signing_key=key,
        )


@pytest.mark.asyncio
async def test_sibling_cancel_completes_teardown_for_every_candidate() -> None:
    """When two candidates fail and the slower one is cancelled *inside* its
    teardown, ``destroy`` must still complete for every candidate.

    Choreography: candidate B fails and enters ``destroy``, which blocks; A then
    fails, which makes the drain cancel B while it is suspended inside destroy.
    A correct reap catches that cancellation and drives the reap to completion
    (a bounded retry, no ``asyncio.shield``), so ``destroy`` completes for both
    candidates rather than leaking B's guest. This is the sibling cancel/drain
    path that had no coverage at all before.
    """
    key = Ed25519PrivateKey.generate()

    class _FakeSession:
        def __init__(self) -> None:
            self.session_id = "s"
            self.slot: int | None = None

        async def snapshot(self) -> str:  # pragma: no cover - candidates fail first
            return "0" * 64

    class _SiblingBackend:
        name = "microvm"

        def __init__(self) -> None:
            self.destroy_completed = 0
            self._b_destroy_calls = 0
            self.b_inside_destroy = asyncio.Event()

        async def resume(self, snapshot_id: str) -> _FakeSession:
            return _FakeSession()

        async def destroy(self, session: _FakeSession) -> None:
            if session.slot == 1:  # B, the slower candidate
                self._b_destroy_calls += 1
                if self._b_destroy_calls == 1:
                    # Announce we are inside destroy, then block so the drain
                    # cancels us here. The retry (call 2) completes immediately.
                    self.b_inside_destroy.set()
                    await asyncio.sleep(3600)
            self.destroy_completed += 1

    backend = _SiblingBackend()

    async def run_candidate(session: _FakeSession, index: int) -> CandidateResult:
        session.slot = index
        if index == 0:  # A fails only once B is suspended inside its teardown
            await backend.b_inside_destroy.wait()
            raise ValueError("A fail")
        raise ValueError("B fail")  # B fails immediately -> enters destroy

    with pytest.raises(ValueError):
        await fork_race(
            backend=backend,  # type: ignore[arg-type]
            base_snapshot_digest="0" * 64,
            run_candidate=run_candidate,
            k=2,
            signing_key=key,
        )

    # destroy() COMPLETED for every candidate: A once, B on the post-cancel retry.
    assert backend.destroy_completed == 2


@pytest.mark.asyncio
async def test_receipt_isolation_reads_as_request_not_attestation(tmp_path: Path) -> None:
    """The per-candidate ``isolation`` value must not read as a verified claim.

    fork_race never boots or probes the isolation boundary, so it cannot attest
    that the named backend's isolation was in effect. It records the *requested*
    backend, plainly labelled so a receipt reader cannot mistake it for an
    enforced-and-checked posture.
    """
    backend = _backend(tmp_path)
    key = Ed25519PrivateKey.generate()
    base = await _base_snapshot(backend)

    receipt = await fork_race(
        backend=backend,
        base_snapshot_digest=base,
        run_candidate=_run_candidate,
        k=3,
        signing_key=key,
    )

    candidates = receipt_to_dict(receipt)["candidates"]
    assert candidates
    for cand in candidates:
        iso = cand["isolation"]
        assert iso.startswith("requested:")
        assert iso == "requested:microvm"
        # A bare backend name would read as an attestation of enforced isolation.
        assert iso != backend.name


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
