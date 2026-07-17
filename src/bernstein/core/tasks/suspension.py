"""Durable task suspend and resume: attested park receipts (#2552).

A long agent session that must wait on a human -- a mid-flight approval, an
external review, a credential rotation, a dependency landing -- has no way to
stop consuming infrastructure. The pre-spawn approval gate halts a task only
before a worker exists; the post-completion review gate only after it exits;
orchestrator holds stop the orchestrator, not a live worker. So the process,
its worktree sandbox, its parallelism seat, and its budget-envelope reservation
all stay allocated for the entire wait. On a capped pool that reservation
blocks other tasks from dispatching.

This module makes the *suspension itself the artifact*. A park is a pair of
Merkle-chained journal rows plus matching HMAC audit-chain receipts, and every
infrastructure release hangs off the suspend receipt's hash. Without the chain
there is no suspension, only a dead process:

* **The suspend row is the identity.** :func:`record_task_suspension_row`
  appends a suspend row to the task's event journal
  (:class:`~bernstein.core.replay.journal.EventJournal`) with the same row
  discipline as the checkpoint substrate: adapter-native session id, a
  workspace hash over the worktree, the journal head, and the envelope balance
  at park time. The row's ``event_hash`` is the suspension's identity.
* **The receipt binds the hash before any effect.**
  :func:`bernstein.core.security.audit_chain.record_task_suspension` binds that
  hash into the HMAC chain *before* the process is reaped, the sandbox is torn
  down, the seat is returned, or envelope headroom is released. Each release
  references the suspend receipt's own HMAC; :func:`release_resources` refuses
  to run any effect without it (:class:`ReleaseWithoutReceiptError`), fail
  closed.
* **Resume is a deterministic projection.** :func:`decide_resume` reuses the
  checkpointed-retry decision (:func:`~bernstein.core.tasks.checkpoint_retry.decide_retry`):
  same workspace hash and a live native session gives ``warm``; a stale session
  or drifted workspace downgrades to ``fork`` or ``cold`` with a recorded
  reason, never silently. Two hosts with the same suspend row and adapter
  capability derive the byte-identical decision, including its ``decision_hash``.
* **The receipt pair is the continuity proof.**
  :func:`verify_suspension_continuity` checks, offline from a copied chain,
  that a resumed task continued from exactly the parked workspace hash, or
  reads the recorded fork/cold downgrade with its reason. Mutating the suspend
  row after the fact fails journal verification at that exact chain position.

Scope relative to steer.pause (#2508): steer.pause is the momentary in-place
halt for quick correction; this is the durable variant that frees
infrastructure and proves continuity. Both share the checkpoint row shape and
the receipt-before-effect rule.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.journal import (
    EventJournal,
    JournalVerifyResult,
    load_events,
    verify_journal,
)
from bernstein.core.tasks.checkpoint_retry import (
    CheckpointRef,
    RetryDecision,
    RetryMode,
    decide_retry,
    task_run_id,
    workspace_hash,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.persistence.work_ledger import LedgerEntry, WorkLedger
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Event-journal row type for a recorded durable suspension (park).
JOURNAL_EVENT_SUSPEND = "task.suspend"

#: Event-journal row type for a recorded durable resume (wake).
JOURNAL_EVENT_RESUME = "task.resume"

#: Canonical resource kinds a park releases, each referencing the suspend
#: receipt hash. ``budget`` is always released (headroom returns to the pool);
#: ``process`` / ``sandbox`` / ``seat`` release when a handle is supplied.
RESOURCE_PROCESS = "process"
RESOURCE_SANDBOX = "sandbox"
RESOURCE_SEAT = "seat"
RESOURCE_BUDGET = "budget"

#: Wake condition composing a park with the pre-spawn approval sentinel: the
#: task resumes only once ``bernstein approve <task-id>`` lands its decision
#: file (see :func:`approval_decision_ref`).
WAKE_APPROVAL = "approval"


class ReleaseWithoutReceiptError(RuntimeError):
    """Raised when an infrastructure release runs without a suspend receipt.

    The suspension is the artifact: every seat return, sandbox teardown,
    process reap, and envelope-headroom release must reference an existing
    ``task.suspend_receipt`` hash. A release with no matching receipt is a
    dead process, not a suspension, so it is rejected before any effect runs.
    """


# ---------------------------------------------------------------------------
# The suspend row (parked-state snapshot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SuspendRow:
    """A journal-anchored snapshot of a parked task's resumable state.

    Every field is folded into the row's ``event_hash`` (the suspension's
    identity) via the journal's timing-excluded payload hash, so two
    byte-identical parks chain to the same head and a later mutation surfaces
    as journal divergence at this exact index.

    Attributes:
        task_id: The task being parked.
        adapter: Registry name of the adapter that owned the session.
        session_id: The native session id to resume from.
        workspace_hash: Content hash of the worktree at park time (the
            safety-valve baseline the resume decision compares against).
        worktree_path: Absolute worktree path the hash was taken over.
        envelope: Quota envelope whose headroom is released.
        reserved_usd: Envelope headroom reserved for the task at park time.
        spent_usd: Spend recorded against the reservation at park time.
        released_usd: Headroom returned to the pool (``max(reserved-spent,0)``).
        wake_condition: ``""`` (operator resume) or :data:`WAKE_APPROVAL`.
        journal_index: 0-based index of the suspend row in the task journal.
        event_hash: Merkle hash of that row -- the suspension's identity.
    """

    task_id: str
    adapter: str
    session_id: str
    workspace_hash: str
    worktree_path: str
    envelope: str
    reserved_usd: float
    spent_usd: float
    released_usd: float
    wake_condition: str
    journal_index: int
    event_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "adapter": self.adapter,
            "session_id": self.session_id,
            "workspace_hash": self.workspace_hash,
            "worktree_path": self.worktree_path,
            "envelope": self.envelope,
            "reserved_usd": self.reserved_usd,
            "spent_usd": self.spent_usd,
            "released_usd": self.released_usd,
            "wake_condition": self.wake_condition,
            "journal_index": self.journal_index,
            "event_hash": self.event_hash,
        }

    def as_checkpoint_ref(self) -> CheckpointRef:
        """Project onto a :class:`CheckpointRef` for the resume decision.

        The durable resume reuses the checkpointed-retry decision, so the
        parked session id and workspace hash are handed to
        :func:`decide_retry` through the same reference shape the retry path
        uses.
        """
        return CheckpointRef(
            task_id=self.task_id,
            adapter=self.adapter,
            session_id=self.session_id,
            workspace_hash=self.workspace_hash,
            worktree_path=self.worktree_path,
            journal_index=self.journal_index,
            event_hash=self.event_hash,
        )


def _journal_path(sdd_dir: Path, task_id: str) -> Path:
    return sdd_dir / "runs" / task_run_id(task_id) / "journal.jsonl"


def record_task_suspension_row(
    *,
    sdd_dir: Path,
    task_id: str,
    adapter: str,
    session_id: str,
    workspace_hash: str,
    worktree_path: str,
    envelope: str,
    reserved_usd: float,
    spent_usd: float,
    released_usd: float,
    wake_condition: str = "",
) -> SuspendRow:
    """Append a suspend row to the task's event journal and return it.

    The row extends the task journal's Merkle chain across processes (opened
    via :meth:`EventJournal.resume`); its ``event_hash`` is the suspension's
    identity, later bound into the audit chain *before* any release runs.

    Raises:
        ValueError: The existing journal fails chain verification.
        RuntimeError: The journal append did not extend the chain.
    """
    journal = EventJournal.resume(task_run_id(task_id), sdd_dir)
    head_before = journal.head()
    journal.record(
        JOURNAL_EVENT_SUSPEND,
        task_id=task_id,
        adapter=adapter,
        session_id=session_id,
        workspace_hash=workspace_hash,
        worktree_path=worktree_path,
        envelope=envelope,
        reserved_usd=reserved_usd,
        spent_usd=spent_usd,
        released_usd=released_usd,
        wake_condition=wake_condition,
    )
    if journal.head() == head_before:
        msg = f"suspend journal append failed for task {task_id!r}"
        raise RuntimeError(msg)
    return SuspendRow(
        task_id=task_id,
        adapter=adapter,
        session_id=session_id,
        workspace_hash=workspace_hash,
        worktree_path=worktree_path,
        envelope=envelope,
        reserved_usd=reserved_usd,
        spent_usd=spent_usd,
        released_usd=released_usd,
        wake_condition=wake_condition,
        journal_index=journal.event_count() - 1,
        event_hash=journal.head(),
    )


def latest_suspension(sdd_dir: Path, task_id: str) -> SuspendRow | None:
    """Return the most recent *verified* suspend row for ``task_id``.

    Fail-closed: the journal's Merkle chain is re-verified before any row is
    trusted. A missing journal, a chain that does not recompute, or the absence
    of any suspend row all return ``None`` -- a tampered suspend row can never
    fuel a resume.
    """
    path = _journal_path(sdd_dir, task_id)
    if not path.exists():
        return None
    result = verify_journal(path)
    if not result.ok:
        logger.warning(
            "suspend journal for task %s failed chain verification at index %s; refusing resume",
            task_id,
            result.divergent_index,
        )
        return None
    for row in reversed(load_events(path)):
        if row.get("event") != JOURNAL_EVENT_SUSPEND:
            continue
        if str(row.get("task_id", "")) != task_id:
            continue
        # A suspend later resumed is still the parked baseline the resume
        # continued from; callers pair it with the resume row for continuity.
        try:
            journal_index = int(row.get("index", -1))
        except (TypeError, ValueError):
            continue
        return SuspendRow(
            task_id=task_id,
            adapter=str(row.get("adapter", "")),
            session_id=str(row.get("session_id", "")),
            workspace_hash=str(row.get("workspace_hash", "")),
            worktree_path=str(row.get("worktree_path", "")),
            envelope=str(row.get("envelope", "")),
            reserved_usd=float(row.get("reserved_usd", 0.0) or 0.0),
            spent_usd=float(row.get("spent_usd", 0.0) or 0.0),
            released_usd=float(row.get("released_usd", 0.0) or 0.0),
            wake_condition=str(row.get("wake_condition", "")),
            journal_index=journal_index,
            event_hash=str(row.get("event_hash", "")),
        )
    return None


# ---------------------------------------------------------------------------
# Infrastructure release (receipt-before-effect, fail closed)
# ---------------------------------------------------------------------------


@dataclass
class ResourceHandles:
    """Physical release effects the orchestrator wires for a real park.

    Each callable performs one release and returns a JSON-safe detail dict
    recorded on its release row. ``None`` means the resource was not allocated
    for this task (skip it). The budget release is intrinsic and always emitted
    -- it needs no handle, only the reservation figures.

    The handles are invoked *only after* :func:`release_resources` has
    validated the suspend receipt hash, so a missing receipt never triggers a
    physical effect.
    """

    reap_process: Callable[[], dict[str, Any]] | None = None
    teardown_sandbox: Callable[[], dict[str, Any]] | None = None
    return_seat: Callable[[], dict[str, Any]] | None = None


@dataclass(frozen=True)
class ReleaseResult:
    """Outcome of :func:`release_resources`.

    Attributes:
        released_usd: Envelope headroom returned to the pool.
        rows: Ordered ``(resource, detail)`` pairs, one per emitted release.
        release_event_hashes: HMACs of the release audit rows, in order.
    """

    released_usd: float
    rows: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    release_event_hashes: list[str] = field(default_factory=list)


def release_resources(
    *,
    chain: AuditChainStore,
    task_id: str,
    suspend_receipt_hash: str,
    envelope: str,
    reserved_usd: float,
    spent_usd: float,
    handles: ResourceHandles | None = None,
) -> ReleaseResult:
    """Release the seat, sandbox, process, and envelope headroom for a park.

    Every release hangs off ``suspend_receipt_hash``; the hash is validated
    *before any physical effect runs*, so a release with no matching receipt is
    rejected and no seat, sandbox, or process is touched. Each release appends a
    ``task.suspend_resource_release`` row to the audit chain referencing the
    receipt, and the budget release additionally emits a chained budget event.

    Args:
        chain: The audit chain store accepting the release rows.
        task_id: The parked task.
        suspend_receipt_hash: HMAC of the ``task.suspend_receipt`` (never
            empty).
        envelope: Envelope whose headroom is released.
        reserved_usd: Reservation held at park time.
        spent_usd: Spend recorded against the reservation at park time.
        handles: Physical release effects; ``None`` releases only budget.

    Raises:
        ReleaseWithoutReceiptError: ``suspend_receipt_hash`` is empty.
    """
    if not suspend_receipt_hash:
        raise ReleaseWithoutReceiptError(
            f"refusing to release resources for task {task_id!r}: no suspend receipt (fail closed)"
        )

    from bernstein.core.cost.budget_actions import build_headroom_release_event
    from bernstein.core.security.audit_chain import record_task_resource_release

    handles = handles or ResourceHandles()
    rows: list[tuple[str, dict[str, Any]]] = []
    hashes: list[str] = []

    def _emit(resource: str, detail: dict[str, Any]) -> None:
        event = record_task_resource_release(
            chain=chain,
            task_id=task_id,
            resource=resource,
            suspend_receipt_hash=suspend_receipt_hash,
            detail=detail,
        )
        rows.append((resource, detail))
        hashes.append(event.hmac)

    # Ordered: reap the running process, tear down its sandbox, return the
    # seat, then release the unspent envelope headroom. Each is gated by the
    # receipt validated above.
    if handles.reap_process is not None:
        _emit(RESOURCE_PROCESS, dict(handles.reap_process()))
    if handles.teardown_sandbox is not None:
        _emit(RESOURCE_SANDBOX, dict(handles.teardown_sandbox()))
    if handles.return_seat is not None:
        _emit(RESOURCE_SEAT, dict(handles.return_seat()))

    budget_event = build_headroom_release_event(
        envelope=envelope,
        reserved_usd=reserved_usd,
        spent_usd=spent_usd,
        suspend_receipt_hash=suspend_receipt_hash,
    )
    _emit(RESOURCE_BUDGET, budget_event.to_dict())

    return ReleaseResult(
        released_usd=budget_event.released_usd,
        rows=rows,
        release_event_hashes=hashes,
    )


# ---------------------------------------------------------------------------
# Park orchestration (row -> receipt -> effects -> ledger)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParkResult:
    """Anchors produced by :func:`park_task`.

    Attributes:
        suspend_row: The journal-anchored parked-state snapshot.
        suspend_receipt_hash: HMAC of the ``task.suspend_receipt`` bound before
            any effect ran.
        release: The infrastructure-release outcome.
        ledger_entry_hash: Work-ledger entry hash for the ``task.suspended``
            transition (``""`` when no ledger was supplied).
    """

    suspend_row: SuspendRow
    suspend_receipt_hash: str
    release: ReleaseResult
    ledger_entry_hash: str


def park_task(
    *,
    sdd_dir: Path,
    task_id: str,
    adapter: str,
    session_id: str,
    worktree_path: Path,
    envelope: str,
    reserved_usd: float,
    spent_usd: float,
    chain: AuditChainStore,
    handles: ResourceHandles | None = None,
    ledger: WorkLedger | None = None,
    wake_condition: str = "",
) -> ParkResult:
    """Durably park ``task_id``: row, receipt, releases, then ledger.

    The order is load-bearing and never reordered:

    1. Compute the workspace hash over the worktree.
    2. Append the suspend row to the task journal (its ``event_hash`` is the
       suspension's identity).
    3. Record the ``task.suspend_receipt`` binding that hash **before any
       effect**.
    4. Release the process, sandbox, seat, and envelope headroom -- each
       referencing the receipt hash, each refused without it.
    5. Persist the ``task.suspended`` transition to the work ledger so the park
       survives an orchestrator restart.

    Args:
        sdd_dir: Project ``.sdd`` directory.
        task_id: The task to park.
        adapter: Adapter that owns the parked session.
        session_id: Native session id to resume from.
        worktree_path: The task's worktree (hashed for the safety valve).
        envelope: Quota envelope whose headroom is released.
        reserved_usd: Envelope headroom reserved for the task.
        spent_usd: Spend recorded against the reservation at park time.
        chain: Audit chain store for the receipt and release rows.
        handles: Physical release effects; ``None`` releases only budget.
        ledger: Optional work ledger to persist the SUSPENDED transition.
        wake_condition: ``""`` or :data:`WAKE_APPROVAL`.

    Returns:
        A :class:`ParkResult` with the row, receipt hash, release outcome, and
        ledger anchor.
    """
    from bernstein.core.cost.budget_actions import compute_released_headroom
    from bernstein.core.persistence.work_ledger import KIND_TASK_SUSPENDED
    from bernstein.core.security.audit_chain import record_task_suspension

    ws_hash = workspace_hash(Path(worktree_path))
    released_usd = compute_released_headroom(reserved_usd, spent_usd)

    suspend_row = record_task_suspension_row(
        sdd_dir=sdd_dir,
        task_id=task_id,
        adapter=adapter,
        session_id=session_id,
        workspace_hash=ws_hash,
        worktree_path=str(worktree_path),
        envelope=envelope,
        reserved_usd=reserved_usd,
        spent_usd=spent_usd,
        released_usd=released_usd,
        wake_condition=wake_condition,
    )

    # Receipt before effect: the suspend receipt exists on the chain before a
    # single resource is freed.
    receipt = record_task_suspension(
        chain=chain,
        task_id=task_id,
        suspend_event_hash=suspend_row.event_hash,
        journal_index=suspend_row.journal_index,
        adapter=adapter,
        workspace_hash=ws_hash,
        envelope=envelope,
        reserved_usd=reserved_usd,
        spent_usd=spent_usd,
        released_usd=released_usd,
        wake_condition=wake_condition,
    )

    release = release_resources(
        chain=chain,
        task_id=task_id,
        suspend_receipt_hash=receipt.hmac,
        envelope=envelope,
        reserved_usd=reserved_usd,
        spent_usd=spent_usd,
        handles=handles,
    )

    ledger_entry_hash = ""
    if ledger is not None:
        entry: LedgerEntry = ledger.append(
            kind=KIND_TASK_SUSPENDED,
            task_id=task_id,
            payload={
                "suspend_event_hash": suspend_row.event_hash,
                "suspend_receipt_hash": receipt.hmac,
                "workspace_hash": ws_hash,
                "envelope": envelope,
                "released_usd": released_usd,
                "wake_condition": wake_condition,
            },
        )
        ledger_entry_hash = entry.entry_hash

    return ParkResult(
        suspend_row=suspend_row,
        suspend_receipt_hash=receipt.hmac,
        release=release,
        ledger_entry_hash=ledger_entry_hash,
    )


# ---------------------------------------------------------------------------
# Resume decision (deterministic projection) + orchestration
# ---------------------------------------------------------------------------


def decide_resume(
    *,
    suspend_row: SuspendRow,
    actual_workspace_hash: str,
    requested_mode: RetryMode | str = RetryMode.WARM,
) -> RetryDecision:
    """Decide warm/fork/cold continuation for a parked task.

    A pure function of the suspend row, the live workspace hash, and the
    adapter capability -- no clock, no network -- so two hosts derive the
    byte-identical :class:`RetryDecision` including its ``decision_hash``. The
    logic is the checkpointed-retry decision (:func:`decide_retry`) applied to
    the parked baseline: same workspace hash and a live session gives warm; a
    drifted workspace or a capability-less adapter downgrades with a recorded
    reason.

    The ``crash`` corrective template is used because a durable park is a
    "continue from where you stopped" resume rather than a gate-failure retry.
    """
    return decide_retry(
        task_id=suspend_row.task_id,
        requested_mode=requested_mode,
        checkpoint=suspend_row.as_checkpoint_ref(),
        actual_workspace_hash=actual_workspace_hash,
        template_id="crash",
        gate_name="suspension",
        gate_output="Task was durably parked; resume from the parked state.",
    )


@dataclass(frozen=True)
class ResumeResult:
    """Anchors produced by :func:`resume_task`.

    Attributes:
        decision: The deterministic continuation decision.
        resume_event_hash: Merkle hash of the resume journal row.
        resume_receipt_hash: HMAC of the ``task.resume_receipt``.
        new_workspace_hash: Content hash of the re-materialized worktree.
        approval_ref: Approval decision digest for an ``--until approval``
            park; ``""`` otherwise.
        ledger_entry_hash: Work-ledger entry hash for the ``task.resumed``
            transition (``""`` when no ledger was supplied).
    """

    decision: RetryDecision
    resume_event_hash: str
    resume_receipt_hash: str
    new_workspace_hash: str
    approval_ref: str
    ledger_entry_hash: str


def resume_task(
    *,
    sdd_dir: Path,
    suspend_row: SuspendRow,
    new_worktree_path: Path,
    chain: AuditChainStore,
    suspend_receipt_hash: str,
    requested_mode: RetryMode | str = RetryMode.WARM,
    ledger: WorkLedger | None = None,
    approval_ref: str = "",
) -> ResumeResult:
    """Durably resume a parked task from its suspend row.

    Re-materializes the continuation decision, appends a resume row binding the
    suspend row it continued from, and records the ``task.resume_receipt`` that
    closes the continuity proof. Deterministic: given the same suspend row and
    adapter capability, the decision hash is byte-identical across hosts.

    Args:
        sdd_dir: Project ``.sdd`` directory.
        suspend_row: The parked-state snapshot to continue from.
        new_worktree_path: The re-materialized worktree (hashed live).
        chain: Audit chain store for the resume receipt.
        suspend_receipt_hash: HMAC of the suspend receipt being continued.
        requested_mode: Operator-requested continuation mode (default warm).
        ledger: Optional work ledger to persist the RESUMED transition.
        approval_ref: Approval decision digest for an ``--until approval`` park.

    Returns:
        A :class:`ResumeResult` with the decision and both continuity anchors.
    """
    from bernstein.core.persistence.work_ledger import KIND_TASK_RESUMED
    from bernstein.core.security.audit_chain import record_task_resume

    new_ws_hash = workspace_hash(Path(new_worktree_path))
    decision = decide_resume(
        suspend_row=suspend_row,
        actual_workspace_hash=new_ws_hash,
        requested_mode=requested_mode,
    )

    journal = EventJournal.resume(task_run_id(suspend_row.task_id), sdd_dir)
    head_before = journal.head()
    journal.record(
        JOURNAL_EVENT_RESUME,
        task_id=suspend_row.task_id,
        continued_from_event_hash=suspend_row.event_hash,
        suspend_receipt_hash=suspend_receipt_hash,
        effective_mode=str(decision.effective_mode),
        requested_mode=str(decision.requested_mode),
        workspace_match=decision.workspace_match,
        new_workspace_hash=new_ws_hash,
        downgrade_reason=decision.downgrade_reason,
        decision_hash=decision.decision_hash,
        approval_ref=approval_ref,
    )
    if journal.head() == head_before:
        msg = f"resume journal append failed for task {suspend_row.task_id!r}"
        raise RuntimeError(msg)
    resume_event_hash = journal.head()

    receipt = record_task_resume(
        chain=chain,
        task_id=suspend_row.task_id,
        suspend_receipt_hash=suspend_receipt_hash,
        suspend_event_hash=suspend_row.event_hash,
        resume_event_hash=resume_event_hash,
        journal_index=journal.event_count() - 1,
        effective_mode=str(decision.effective_mode),
        requested_mode=str(decision.requested_mode),
        workspace_match=decision.workspace_match,
        new_workspace_hash=new_ws_hash,
        downgrade_reason=decision.downgrade_reason,
        decision_hash=decision.decision_hash,
        approval_ref=approval_ref,
    )

    ledger_entry_hash = ""
    if ledger is not None:
        entry: LedgerEntry = ledger.append(
            kind=KIND_TASK_RESUMED,
            task_id=suspend_row.task_id,
            payload={
                "continued_from_event_hash": suspend_row.event_hash,
                "suspend_receipt_hash": suspend_receipt_hash,
                "resume_receipt_hash": receipt.hmac,
                "effective_mode": str(decision.effective_mode),
                "new_workspace_hash": new_ws_hash,
                "decision_hash": decision.decision_hash,
            },
        )
        ledger_entry_hash = entry.entry_hash

    return ResumeResult(
        decision=decision,
        resume_event_hash=resume_event_hash,
        resume_receipt_hash=receipt.hmac,
        new_workspace_hash=new_ws_hash,
        approval_ref=approval_ref,
        ledger_entry_hash=ledger_entry_hash,
    )


# ---------------------------------------------------------------------------
# Approval composition (--until approval)
# ---------------------------------------------------------------------------


def approval_decision_ref(workdir: Path, task_id: str) -> str:
    """Return the approval decision digest for a woken ``--until approval`` park.

    The digest binds the task id and the content of the
    ``<task_id>.approved`` decision file written by ``bernstein approve``. It
    is empty when no approval decision exists yet, so a resume gated on
    approval can refuse to proceed until the operator lands the decision. The
    same digest is written into the resume receipt, so the approval record and
    the resume receipt reference each other.
    """
    approved = workdir / ".sdd" / "runtime" / "approvals" / f"{task_id}.approved"
    if not approved.exists():
        return ""
    try:
        content = approved.read_bytes()
    except OSError:
        return ""
    digest = hashlib.sha256(b"approval:" + task_id.encode("utf-8") + b":" + content).hexdigest()
    return digest


def write_resume_marker(workdir: Path, task_id: str, resume_receipt_hash: str) -> Path:
    """Write a ``<task_id>.resumed`` marker referencing the resume receipt.

    Closes the approval<->resume back-reference: ``bernstein approve`` lands the
    ``.approved`` decision the resume receipt binds, and this marker lands the
    resume receipt hash the approval record can be checked against. Best-effort;
    a write failure is logged and the marker path returned regardless.
    """
    approvals = workdir / ".sdd" / "runtime" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    marker = approvals / f"{task_id}.resumed"
    try:
        marker.write_text(resume_receipt_hash, encoding="utf-8")
    except OSError as exc:  # pragma: no cover -- defensive
        logger.warning("failed to write resume marker for task %s: %s", task_id, type(exc).__name__)
    return marker


# ---------------------------------------------------------------------------
# Offline continuity verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContinuityResult:
    """Outcome of :func:`verify_suspension_continuity`.

    Attributes:
        ok: ``True`` only when every check passed: audit HMAC chain intact,
            task journal Merkle chain intact, and a resume receipt that
            continued from exactly the parked suspend row.
        chain_ok: Whether the HMAC audit chain verified.
        journal_ok: Whether the task journal Merkle chain verified.
        resumed: Whether a resume receipt for the task was found.
        effective_mode: The recorded continuation mode (``warm`` / ``fork`` /
            ``cold``), or ``""`` when not resumed.
        workspace_match: Whether the resume continued from the parked
            workspace hash.
        downgrade_reason: Recorded fork/cold reason, or ``""``.
        errors: Human-readable explanations of any failure.
    """

    ok: bool
    chain_ok: bool
    journal_ok: bool
    resumed: bool
    effective_mode: str
    workspace_match: bool
    downgrade_reason: str
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "chain_ok": self.chain_ok,
            "journal_ok": self.journal_ok,
            "resumed": self.resumed,
            "effective_mode": self.effective_mode,
            "workspace_match": self.workspace_match,
            "downgrade_reason": self.downgrade_reason,
            "errors": list(self.errors),
        }


def _journal_verify(sdd_dir: Path, task_id: str) -> JournalVerifyResult:
    return verify_journal(_journal_path(sdd_dir, task_id))


def verify_suspension_continuity(
    *,
    sdd_dir: Path,
    task_id: str,
    chain: AuditChainStore,
) -> ContinuityResult:
    """Prove, offline, that a resumed task continued from the parked state.

    The check is the continuity proof AC (#2552): from a copied chain and the
    task journal alone, confirm

    1. the HMAC audit chain verifies (a mutated receipt fails at its position);
    2. the task journal Merkle chain verifies (a mutated suspend row fails at
       its exact index);
    3. a ``task.resume_receipt`` exists that references the ``suspend_receipt``
       whose ``suspend_event_hash`` equals the parked suspend row's identity,
       and the recorded continuation mode / workspace match / downgrade reason
       describe how it continued (warm from the parked hash, or a recorded fork
       or cold downgrade with its reason).

    No worker, no network, no live worktree is required -- everything is read
    from the chain and the journal.
    """
    from bernstein.core.security.audit_chain import EVENT_TASK_RESUMED, EVENT_TASK_SUSPENDED

    errors: list[str] = []

    chain_ok, chain_errors = chain.verify()
    if not chain_ok:
        errors.extend(chain_errors)

    journal_result = _journal_verify(sdd_dir, task_id)
    journal_ok = journal_result.ok
    if not journal_ok:
        errors.append(
            f"task journal chain broke at index {journal_result.divergent_index}: "
            f"{'; '.join(journal_result.errors) or 'verification failed'}"
        )

    suspend_events = [e for e in chain.query(event_type=EVENT_TASK_SUSPENDED) if e.details.get("task_id") == task_id]
    resume_events = [e for e in chain.query(event_type=EVENT_TASK_RESUMED) if e.details.get("task_id") == task_id]

    if not suspend_events:
        errors.append(f"no suspend receipt found for task {task_id!r}")
        return ContinuityResult(
            ok=False,
            chain_ok=chain_ok,
            journal_ok=journal_ok,
            resumed=False,
            effective_mode="",
            workspace_match=False,
            downgrade_reason="",
            errors=errors,
        )

    suspend_event = suspend_events[-1]
    parked_hash = str(suspend_event.details.get("suspend_event_hash", ""))

    resumed = bool(resume_events)
    effective_mode = ""
    workspace_match = False
    downgrade_reason = ""
    if resumed:
        resume_event = resume_events[-1]
        effective_mode = str(resume_event.details.get("effective_mode", ""))
        workspace_match = bool(resume_event.details.get("workspace_match", False))
        downgrade_reason = str(resume_event.details.get("downgrade_reason", ""))
        continued_from = str(resume_event.details.get("suspend_event_hash", ""))
        if continued_from != parked_hash:
            errors.append(
                "resume receipt did not continue from the parked suspend row "
                f"(continued_from={continued_from[:16]}..., parked={parked_hash[:16]}...)"
            )
        # A warm continuation asserts the parked workspace hash matched; a
        # downgrade must carry a reason so the fork/cold is never silent.
        if effective_mode == str(RetryMode.WARM) and not workspace_match:
            errors.append("warm resume recorded without a workspace-hash match")
        if effective_mode != str(RetryMode.WARM) and not downgrade_reason:
            errors.append(f"{effective_mode} downgrade recorded without a reason")

    ok = chain_ok and journal_ok and not errors
    return ContinuityResult(
        ok=ok,
        chain_ok=chain_ok,
        journal_ok=journal_ok,
        resumed=resumed,
        effective_mode=effective_mode,
        workspace_match=workspace_match,
        downgrade_reason=downgrade_reason,
        errors=errors,
    )


__all__ = [
    "JOURNAL_EVENT_RESUME",
    "JOURNAL_EVENT_SUSPEND",
    "RESOURCE_BUDGET",
    "RESOURCE_PROCESS",
    "RESOURCE_SANDBOX",
    "RESOURCE_SEAT",
    "WAKE_APPROVAL",
    "ContinuityResult",
    "ParkResult",
    "ReleaseResult",
    "ReleaseWithoutReceiptError",
    "ResourceHandles",
    "ResumeResult",
    "SuspendRow",
    "approval_decision_ref",
    "decide_resume",
    "latest_suspension",
    "park_task",
    "record_task_suspension_row",
    "release_resources",
    "resume_task",
    "verify_suspension_continuity",
    "write_resume_marker",
]
