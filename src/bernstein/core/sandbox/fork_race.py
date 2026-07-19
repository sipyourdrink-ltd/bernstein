"""Deterministic fork-and-race over one content-addressed base snapshot (#2613).

``best_of_n`` today spawns K candidate workers from *scratch* in separate
worktrees, so the K attempts never actually start from one identical
captured state - rerun the race and the base differs, so the winner is not
attributable to the candidate's work alone. :func:`fork_race` closes that
gap. It resumes K candidate sessions from the *same* content-addressed
base snapshot digest, runs each to a terminal snapshot, and selects the
winner with the existing deterministic ranker
(:func:`bernstein.core.orchestration.best_of_n.select_winner` backed by
TOPSIS) - **no LLM in the selection path**. The output is a signed
:class:`~bernstein.core.sandbox.selection_receipt.SelectionReceipt` that
reconstructs the whole race offline.

Two determinism disciplines are load-bearing and easy to get wrong:

- **Rank on a wall-clock-free profile.** :data:`DETERMINISTIC_PROFILE`
  ranks on ``correctness``/``cost``/``reversibility`` only. It deliberately
  omits the ``latency`` axis, which
  :func:`best_of_n._to_rank_candidate` would populate from ``runtime_s`` -
  a host wall-clock measurement that differs every run and could flip the
  winner on scheduler jitter, breaking the byte-identical-receipt gate.
- **Fix candidate order before ranking, not just before serialising.**
  TOPSIS sums over the candidate matrix, and float addition is not
  associative, so two runs whose candidate submission order differs can
  produce last-bit-different scores. Candidates are sorted by ``task_id``
  *before* they reach ``select_winner``, so the ranking input is identical
  across runs.

The audit append is a single serialised call after all K candidates and
the ranker have finished (``AuditLog`` has no internal lock; a per-candidate
fan-out would race ``prev_hmac`` and corrupt the chain). Publication is
crash-safe: CAS blobs are already stored by ``snapshot()``, then the
receipt is signed, then the audit entry lands, then the receipt file is
exposed via tmp+rename - so a crash never leaves a validly-signed but
unanchored receipt visible.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from typing import TYPE_CHECKING, Any, Protocol

from bernstein.core.orchestration.best_of_n import CandidateResult, select_winner
from bernstein.core.orchestration.multi_criteria_rank import (
    CriterionProfile,
    build_criterion_profile,
)
from bernstein.core.sandbox.selection_receipt import (
    RaceCandidate,
    SelectionReceipt,
    build_selection_receipt,
    sign_receipt,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.sandbox.backend import SandboxSession
    from bernstein.core.security.audit import AuditLog

#: The deterministic ranking profile fork-race pins. No wall-clock axis:
#: ``latency`` (which maps from ``runtime_s``) is deliberately excluded so
#: the winner is a pure function of the candidates' deliverables.
DETERMINISTIC_PROFILE: CriterionProfile = build_criterion_profile(
    ["correctness", "cost", "reversibility"],
)

_logger = logging.getLogger(__name__)

#: Audit event-type for a completed fork-race selection.
FORK_RACE_EVENT_TYPE = "sandbox.fork_race"

#: Prefix stamped on each candidate's receipt ``isolation`` value. See
#: :func:`_requested_isolation`.
_REQUESTED_ISOLATION_PREFIX = "requested:"

#: Serialises concurrent same-process audit appends. ``AuditLog`` has no
#: internal lock, and offloading the append to ``asyncio.to_thread`` means two
#: concurrent ``fork_race()`` calls sharing one ``AuditLog`` would run
#: ``AuditLog.log`` in parallel worker threads - both reading the same
#: ``prev_hmac`` and forking the chain. The cross-process flock only guards
#: *separate processes* (and is a no-op when ``audit_lock_path`` is ``None``),
#: so an in-process lock is also required now that the append runs off-loop.
_AUDIT_APPEND_LOCK = threading.Lock()


@contextlib.contextmanager
def _cross_process_audit_lock(lock_path: Path | None) -> Iterator[None]:
    """Serialise the single audit append across concurrent fork-race *processes*.

    In-process concurrency is handled separately by :data:`_AUDIT_APPEND_LOCK`
    (needed because the append now runs in a worker thread via
    ``asyncio.to_thread``). This guards the remaining window: two *separate
    processes* running a fork-race against the same audit directory, where
    ``AuditLog`` has no lock of its own and both could read the same
    ``prev_hmac`` and fork
    the chain. A no-op when *lock_path* is ``None`` or on a platform without
    ``fcntl`` (e.g. Windows).
    """
    if lock_path is None:
        yield
        return
    try:
        import fcntl
    except ImportError:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class ForkRaceBackend(Protocol):
    """The slice of a sandbox backend :func:`fork_race` needs.

    Any SNAPSHOT-capable backend whose ``snapshot()`` returns a CAS digest
    satisfies this; in practice that is
    :class:`~bernstein.core.sandbox.backends.microvm.MicroVMSandboxBackend`.
    """

    name: str

    async def resume(self, snapshot_id: str) -> SandboxSession: ...

    async def destroy(self, session: SandboxSession) -> None: ...


def _requested_isolation(backend: ForkRaceBackend) -> str:
    """Return the value stored in each candidate's receipt ``isolation`` field.

    fork_race does not boot or probe the isolation boundary, so it cannot attest
    that the named backend's isolation was actually in effect for the run.
    Recording a bare backend name (which defaults to ``"microvm"`` in the
    receipt schema) reads as exactly such an attestation. Prefixing it with
    ``requested:`` makes the value plainly a *request*, not a verified posture,
    so a receipt reader cannot mistake it for an enforced-and-checked isolation
    claim. The genuinely enforced-and-checked posture - content-addressed,
    integrity-verified snapshots - is attested by the base and terminal digests
    the CAS verifier re-hashes, not by this field.

    (The field is *named* ``isolation`` in
    :mod:`bernstein.core.sandbox.selection_receipt`; this change is confined to
    the value fork_race supplies for it.)
    """
    return f"{_REQUESTED_ISOLATION_PREFIX}{backend.name}"


async def _reap_session(backend: ForkRaceBackend, session: SandboxSession) -> None:
    """Tear ``session`` down, completing the reap even under a mid-teardown cancel.

    When a sibling candidate fails, :func:`fork_race` cancels the remaining
    candidates; that cancel can land while this task is suspended *inside*
    ``backend.destroy``. Leaving a half-reclaimed guest is the leak this guards:
    catch the cancellation, drive one more bounded reap to completion, then
    re-raise the cancellation so the task still terminates. No ``asyncio.shield``
    - shielding an unbounded reap turns a cancel into a hang; a single bounded
    retry does not. A non-cancellation ``destroy`` error is left to propagate to
    the caller, which decides whether it masks a candidate failure or is itself
    the error worth surfacing.
    """
    try:
        await backend.destroy(session)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await backend.destroy(session)
        raise


def _score_vector(result: CandidateResult) -> dict[str, float]:
    """Project a candidate onto the deterministic axes recorded in the receipt.

    Mirrors :func:`best_of_n._to_rank_candidate` for the wall-clock-free
    axes only, so the vector stored in the receipt matches what the ranker
    actually consumed.
    """
    correctness = 1.0 if result.tests_passing else 0.0
    judge = result.judge_score if result.judge_score is not None else correctness
    correctness = max(0.0, min(1.0, 0.5 * correctness + 0.5 * judge))
    return {
        "correctness": correctness,
        "cost": max(0.0, 1.0 - max(0.0, min(1.0, result.lint_score))),
        "reversibility": 1.0,
    }


def _profile_to_dict(profile: CriterionProfile) -> dict[str, Any]:
    return {
        "method": "topsis",
        "criteria": [{"name": c.name, "direction": c.direction, "weight": c.weight} for c in profile.criteria],
    }


def _require_unique_task_ids(task_ids: Iterable[str]) -> None:
    """Reject empty or duplicate candidate task ids.

    ``task_id`` keys both the terminal-digest map and the receipt's candidate
    map, so a collision would silently drop a candidate's terminal snapshot from
    the signed receipt (and from ``snapshot_digests`` verification) - the race
    the receipt attests could then no longer be reconstructed or attributed.
    """
    seen: set[str] = set()
    dupes: set[str] = set()
    for tid in task_ids:
        if not tid:
            raise ValueError("fork_race candidate task_ids must be non-empty")
        if tid in seen:
            dupes.add(tid)
        seen.add(tid)
    if dupes:
        raise ValueError(f"fork_race candidate task_ids must be unique; duplicates: {sorted(dupes)}")


async def fork_race(
    *,
    backend: ForkRaceBackend,
    base_snapshot_digest: str,
    run_candidate: Callable[[SandboxSession, int], Awaitable[CandidateResult]],
    k: int,
    signing_key: Ed25519PrivateKey,
    profile: CriterionProfile | None = None,
    audit_log: AuditLog | None = None,
    audit_lock_path: Path | None = None,
    actor: str = "fork_race",
) -> SelectionReceipt:
    """Fork K candidates from one base snapshot and return a signed receipt.

    Args:
        backend: A SNAPSHOT-capable backend whose ``snapshot()`` returns a
            CAS digest (the microVM backend).
        base_snapshot_digest: The single content-addressed base every
            candidate forks from - the anchor that makes the race
            attributable.
        run_candidate: Async callback that mutates a resumed session and
            returns its :class:`CandidateResult` (task_id + deterministic
            scores). It must not snapshot; fork_race captures the terminal
            snapshot after it returns.
        k: Number of candidates (>= 1).
        signing_key: Ed25519 private key that signs the receipt.
        profile: Ranking profile. Defaults to :data:`DETERMINISTIC_PROFILE`.
        audit_log: When provided, the receipt is appended to the HMAC audit
            chain in exactly one serialised call after ranking.
        audit_lock_path: Optional lock file guarding the audit append against
            concurrent fork-race *processes* sharing the audit directory (the
            in-process append is already atomic). No-op when ``None``.
        actor: Actor recorded on the audit entry.

    Returns:
        The signed :class:`SelectionReceipt`.

    Raises:
        ValueError: When *k* < 1.
    """
    if k < 1:
        raise ValueError(f"fork_race requires k >= 1, got {k}")
    ranking_profile = profile or DETERMINISTIC_PROFILE
    pub = signing_key.public_key()

    async def _one(index: int) -> tuple[CandidateResult, str]:
        session = await backend.resume(base_snapshot_digest)
        try:
            result = await run_candidate(session, index)
            terminal_digest = await session.snapshot()
        except BaseException:
            # Preserve the ORIGINAL candidate failure verbatim. The reap must
            # still complete - even if the outer drain cancels this task
            # mid-teardown (:func:`_reap_session` drives it to completion) - but
            # a reap error, including a ``BaseException`` such as a cancellation
            # injected into destroy(), must never replace the in-flight candidate
            # exception the drain re-raises. Catch ``BaseException`` around the
            # reap and log a genuine teardown failure so a leaked guest leaves a
            # trace instead of vanishing.
            try:
                await _reap_session(backend, session)
            except BaseException:
                _logger.warning(
                    "candidate session teardown failed during fork_race cleanup; "
                    "a guest resource may have leaked (candidate index %d)",
                    index,
                    exc_info=True,
                )
            raise
        # Success path: the reap gets the same cancellation-resilient teardown,
        # but here a destroy() failure IS the error worth surfacing, so it
        # propagates out of _reap_session rather than being swallowed.
        await _reap_session(backend, session)
        return result, terminal_digest

    # Race the candidates concurrently; the barrier here is intentional -
    # ranking and the single audit append need the full result set. If any
    # candidate raises, gather surfaces the first error but leaves its siblings
    # running in the (persistent) event loop - explicitly cancel and drain them
    # so no in-flight candidate outlives the race and every _one finally-block
    # (which destroys its session) still runs.
    tasks = [asyncio.ensure_future(_one(i)) for i in range(k)]
    try:
        outcomes = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    # D2: fix candidate order BEFORE ranking so the TOPSIS matrix (and its
    # float sums) is identical across runs - not merely before serialising.
    ordered = sorted(outcomes, key=lambda pair: pair[0].task_id)
    results = [result for result, _ in ordered]

    # Task ids must be unique and non-empty: terminal_by_id (and the receipt's
    # candidate map) key on task_id, so a duplicate would silently collapse a
    # candidate's terminal snapshot out of the signed attestation.
    _require_unique_task_ids(result.task_id for result in results)
    terminal_by_id = {result.task_id: digest for result, digest in ordered}

    winner = select_winner(results, profile=ranking_profile)

    race_candidates = [
        RaceCandidate(
            task_id=result.task_id,
            terminal_snapshot_digest=terminal_by_id[result.task_id],
            score_vector=_score_vector(result),
            isolation=_requested_isolation(backend),
        )
        for result in results
    ]

    receipt = build_selection_receipt(
        base_snapshot_digest=base_snapshot_digest,
        candidates=race_candidates,
        winner_task_id=winner.task_id,
        ranker_profile=_profile_to_dict(ranking_profile),
        public_key=pub,
    )
    signed = sign_receipt(receipt, private_key=signing_key)

    # Single serialised audit append AFTER all candidates + ranking. The
    # receipt body is chain-position-agnostic; this wrapper entry is what
    # binds it into the tamper-evident chain (bound by its own prev_hmac).
    if audit_log is not None:
        from bernstein.core.sandbox.selection_receipt import receipt_to_dict

        # The cross-process flock can block on contention from another fork-race
        # process, and audit_log.log does a synchronous file write - both would
        # stall the caller's event loop (fork_race is a library coroutine that
        # may be awaited alongside other tasks). Offload the lock+append to a
        # worker thread. It is still awaited before returning, so the crash-safe
        # ordering (CAS blobs -> sign -> audit -> receipt file) holds, and lock
        # and append stay together in one call, so the chain's prev_hmac is
        # still written under exclusive serialisation.
        def _append() -> None:
            with _AUDIT_APPEND_LOCK, _cross_process_audit_lock(audit_lock_path):
                audit_log.log(
                    FORK_RACE_EVENT_TYPE,
                    actor,
                    "sandbox_selection_receipt",
                    signed.payload_digest,
                    {
                        "base_snapshot_digest": signed.base_snapshot_digest,
                        "winner_task_id": signed.winner_task_id,
                        "winner_snapshot_digest": signed.winner_snapshot_digest,
                        "keyid": signed.keyid,
                        "receipt": receipt_to_dict(signed),
                    },
                )

        await asyncio.to_thread(_append)

    return signed


__all__ = [
    "DETERMINISTIC_PROFILE",
    "FORK_RACE_EVENT_TYPE",
    "ForkRaceBackend",
    "fork_race",
]
