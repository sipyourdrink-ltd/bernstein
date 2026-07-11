"""RunService: the lifecycle surface over the durable work ledger (#2352).

A detached run is owned by a supervisor daemon, but its authoritative state is
the hash-chained work ledger, not the process. :class:`RunService` is the thin
control surface that opens a run, records every lifecycle boundary as a signed
receipt in the HMAC audit chain, and -- on attach -- proves the current ledger
head extends the head the operator last saw before rendering live state.

Every method here is a projection of, or an append to, one of two chains: the
work ledger (run/task transitions) and the audit chain (lifecycle receipts).
The service never keeps mutable in-memory scheduler state; two operators
replaying the same ledger arrive at byte-identical projections.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.persistence.work_ledger import (
    GENESIS_HASH,
    KIND_RUN_CLOSED,
    KIND_RUN_OPEN,
    KIND_TASK_SCHEDULED,
    LedgerReader,
    LedgerState,
    WorkLedger,
    replay_state,
    run_ledger_dir,
)
from bernstein.core.run_service.descriptor import RunDescriptor
from bernstein.core.run_service.paths import descriptor_path, run_dir, validate_run_id
from bernstein.core.run_service.receipts import (
    TRANSITION_COMPLETED,
    TRANSITION_DAEMON_RESTARTED,
    TRANSITION_DETACHED,
    TRANSITION_REATTACHED,
    TRANSITION_SUBMITTED,
    ContinuityProof,
    prove_continuity,
)
from bernstein.core.security.audit_chain import (
    EVENT_RUN_LIFECYCLE,
    AuditChainStore,
    record_run_lifecycle,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.security.audit import AuditEvent


class RunServiceError(RuntimeError):
    """Raised for unknown runs and other run-service control failures."""


@dataclass(frozen=True)
class RunHandle:
    """The result of :meth:`RunService.submit`."""

    run_id: str
    ledger_head: str
    entry_count: int
    descriptor: RunDescriptor


@dataclass(frozen=True)
class AttachResult:
    """The result of :meth:`RunService.attach`."""

    run_id: str
    state: LedgerState
    proof: ContinuityProof
    current_head: str
    receipt: AuditEvent


class RunService:
    """Lifecycle control surface for detached runs, rooted at a project dir."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._sdd = self._root / ".sdd"

    # -- paths --------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def _ledger_dir(self, run_id: str) -> Path:
        return run_ledger_dir(self._sdd, run_id)

    def _reader(self, run_id: str) -> LedgerReader:
        return LedgerReader(self._ledger_dir(run_id))

    def _chain(self) -> AuditChainStore:
        return AuditChainStore(self._sdd / "audit")

    def _exists(self, run_id: str) -> bool:
        return self._reader(run_id).exists() or descriptor_path(self._sdd, run_id).exists()

    def _require(self, run_id: str) -> None:
        if not self._exists(run_id):
            msg = f"no detached run {run_id!r} (submit it first)"
            raise RunServiceError(msg)

    # -- lifecycle ----------------------------------------------------------

    def submit(self, goal: str, task_ids: Sequence[str], *, run_id: str | None = None) -> RunHandle:
        """Open a run: seed the ledger, persist the descriptor, sign a receipt.

        The goal is decomposed into *task_ids* by the caller (the planner); the
        ledger records ``run.open`` plus one ``task.scheduled`` per task. The
        goal text itself never enters the portable ledger -- only its digest.
        """
        run_id = validate_run_id(run_id or f"run-{uuid.uuid4().hex[:12]}")
        descriptor = RunDescriptor(
            run_id=run_id,
            goal=goal,
            task_ids=[str(t) for t in task_ids],
            created_ts=time.time(),
        )

        run_dir(self._sdd, run_id).mkdir(parents=True, exist_ok=True)
        ledger = WorkLedger.open(self._ledger_dir(run_id))
        try:
            ledger.append(
                kind=KIND_RUN_OPEN,
                payload={
                    "run_id": run_id,
                    "goal_sha256": descriptor.goal_sha256,
                    "task_count": len(descriptor.task_ids),
                },
            )
            for task_id in descriptor.task_ids:
                ledger.append(kind=KIND_TASK_SCHEDULED, task_id=task_id)
            head = ledger.head_hash
            entry_count = ledger.next_seq
        finally:
            ledger.close()

        descriptor.write(descriptor_path(self._sdd, run_id))
        self._record(run_id, TRANSITION_SUBMITTED, ledger_head=head, entry_count=entry_count)
        return RunHandle(run_id=run_id, ledger_head=head, entry_count=entry_count, descriptor=descriptor)

    def detach(self, run_id: str) -> AuditEvent:
        """Record a detach boundary: the operator drops the terminal.

        The run keeps executing under its supervisor; this only marks the
        ledger head the operator last saw so a later attach can prove the run
        did not diverge while they were away.
        """
        self._require(run_id)
        head, count = self._head_and_count(run_id)
        return self._record(run_id, TRANSITION_DETACHED, ledger_head=head, entry_count=count)

    def attach(self, run_id: str) -> AttachResult:
        """Reattach: prove continuity across the boundary, then project state.

        Returns an :class:`AttachResult` whose ``proof`` is False (rather than
        raising) when the ledger diverged or failed to verify, so the caller
        can surface the exact reason. A ``reattached`` receipt is always
        recorded so the reattach itself is chain-attested.
        """
        self._require(run_id)
        boundary = self._last_boundary_head(run_id)
        reader = self._reader(run_id)
        proof = prove_continuity(reader, boundary, run_id=run_id)
        state = replay_state(reader.entries(), run_id=run_id)
        receipt = self._record(
            run_id,
            TRANSITION_REATTACHED,
            ledger_head=proof.current_head,
            entry_count=reader.verify().entries,
            from_head=boundary,
            to_head=proof.current_head,
            entries_added=proof.entries_added,
        )
        return AttachResult(
            run_id=run_id,
            state=state,
            proof=proof,
            current_head=proof.current_head,
            receipt=receipt,
        )

    def daemon_restart(self, run_id: str) -> AuditEvent:
        """Record a supervisor (re)start that resumes an existing ledger."""
        self._require(run_id)
        boundary = self._last_boundary_head(run_id)
        reader = self._reader(run_id)
        proof = prove_continuity(reader, boundary, run_id=run_id)
        return self._record(
            run_id,
            TRANSITION_DAEMON_RESTARTED,
            ledger_head=proof.current_head,
            entry_count=reader.verify().entries,
            from_head=boundary,
            to_head=proof.current_head,
            entries_added=proof.entries_added,
        )

    def complete(self, run_id: str) -> AuditEvent:
        """Close the run in the ledger and record a completion receipt."""
        self._require(run_id)
        reader = self._reader(run_id)
        state = replay_state(reader.entries(), run_id=run_id)
        if not state.run_closed:
            ledger = WorkLedger.open(self._ledger_dir(run_id))
            try:
                ledger.append(kind=KIND_RUN_CLOSED, payload={"run_id": run_id})
            finally:
                ledger.close()
        head, count = self._head_and_count(run_id)
        return self._record(run_id, TRANSITION_COMPLETED, ledger_head=head, entry_count=count)

    # -- projection ---------------------------------------------------------

    def project(self, run_id: str) -> LedgerState:
        """Return the read-only ledger projection (no receipt recorded)."""
        self._require(run_id)
        reader = self._reader(run_id)
        return replay_state(reader.entries(), run_id=run_id)

    def lifecycle_events(self, run_id: str) -> list[AuditEvent]:
        """Return this run's lifecycle receipts in chronological order."""
        chain = self._chain()
        return [e for e in chain.query(event_type=EVENT_RUN_LIFECYCLE) if e.details.get("run_id") == run_id]

    # -- internals ----------------------------------------------------------

    def _head_and_count(self, run_id: str) -> tuple[str, int]:
        result = self._reader(run_id).verify()
        return result.head_hash, result.entries

    def _last_boundary_head(self, run_id: str) -> str:
        """Return the ledger head recorded by the newest lifecycle receipt."""
        events = self.lifecycle_events(run_id)
        if not events:
            return GENESIS_HASH
        head = events[-1].details.get("ledger_head")
        return str(head) if head else GENESIS_HASH

    def _record(
        self,
        run_id: str,
        transition: str,
        *,
        ledger_head: str,
        entry_count: int,
        from_head: str = "",
        to_head: str = "",
        entries_added: int = 0,
    ) -> AuditEvent:
        return record_run_lifecycle(
            chain=self._chain(),
            run_id=run_id,
            transition=transition,
            ledger_head=ledger_head,
            entry_count=entry_count,
            from_head=from_head,
            to_head=to_head,
            entries_added=entries_added,
        )


__all__ = [
    "AttachResult",
    "RunHandle",
    "RunService",
    "RunServiceError",
]
