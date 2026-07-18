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
  row after the fact fails journal verification at that exact chain position,
  and unrelated evidence (a resume receipt hanging off another park, a receipt
  naming a row the journal never held, a park settled twice) is refused rather
  than reported as verified. A park that has not settled yet reports
  ``pending`` rather than failing: a live park is an incomplete lifecycle, not
  a broken proof.
* **A receipt only counts for the park it binds.** Every release and every
  resume resolves the claimed ``task.suspend_receipt`` on the chain and checks
  it names this task and this suspend row (:func:`verify_suspension_receipt`)
  before it mutates anything, so a substituted receipt cannot drive a state
  change. The approval wake gate is enforced in :func:`resume_task` itself,
  and a ``task_id`` that is not a plain identifier never reaches a filesystem
  name.

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

from bernstein.core.orchestration.approval_gate import (
    UnsafeApprovalIdError,
    approval_path,
    validate_approval_id,
)
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
    from bernstein.core.security.audit_chain import AuditChainStore, AuditEvent

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

#: Machine-readable outcomes of :func:`verify_suspension_continuity`. A caller
#: branches on these rather than parsing an error string.
#:
#: A live park and a broken proof are different things and must not collapse
#: into one signal: an operator sweeping a fleet with parked tasks would see
#: every live park as a failure and the real breaks would drown. ``pending``
#: therefore means "this park has not settled yet, so there is nothing to
#: verify", while ``failed`` is reserved for a settlement that actually
#: happened against evidence that does not hold.
CONTINUITY_VERIFIED = "verified"
CONTINUITY_PENDING = "pending"
CONTINUITY_FAILED = "failed"

#: Approvals live under ``<workdir>/.sdd/runtime/approvals``; the same relative
#: root the pre-spawn approval gate writes its sentinels into.
_APPROVALS_REL = Path(".sdd") / "runtime" / "approvals"

#: The identifier rule and the contained-path builder are owned by
#: :mod:`bernstein.core.orchestration.approval_gate`, the module that owns the
#: approvals directory. This module deliberately does not keep its own copy:
#: ``bernstein approve`` / ``reject``, the pre-spawn gate, and the park/resume
#: path all write into the same directory, and a second copy of the rule is a
#: second thing to drift. :class:`UnsafeTaskIdError` is an alias, not a
#: subclass, so a refusal raised by any sink is the same type everywhere.
UnsafeTaskIdError = UnsafeApprovalIdError


class ReleaseWithoutReceiptError(RuntimeError):
    """Raised when an infrastructure release runs without a suspend receipt.

    The suspension is the artifact: every seat return, sandbox teardown,
    process reap, and envelope-headroom release must reference an existing
    ``task.suspend_receipt`` hash. A release with no matching receipt is a
    dead process, not a suspension, so it is rejected before any effect runs.
    """


class SuspendReceiptMismatchError(RuntimeError):
    """Raised when a suspend receipt does not bind the park it is used for.

    A non-empty hash is not evidence. The receipt must exist on the HMAC chain
    as a ``task.suspend_receipt``, name this ``task_id``, and -- when the caller
    knows which park it is continuing -- bind exactly that suspend row's
    ``event_hash`` and journal index. A receipt from a different row, a
    different task, or no row at all is refused before any effect or mutation,
    so a substituted receipt can never drive a state change.
    """


class SuspensionAlreadySettledError(RuntimeError):
    """Raised when a park that already carries a resume receipt is resumed again.

    A suspend receipt settles exactly once. The chain is the record of that
    settlement: if a ``task.resume_receipt`` already hangs off this suspend
    receipt, the park is spent, and a second resume would append another resume
    row for a decision that was already made. That is the replay of a settled
    decision, so it is refused before the journal is touched.

    This is what makes an ``--until approval`` wake gate single-use. The
    approval decision file records *that* the operator approved, not how many
    times the approval may be spent; the settlement record on the chain is what
    bounds it to one. Parking the task again mints a new suspend row and a new
    receipt, which settles once in its own right.
    """


class ResumeApprovalRequiredError(RuntimeError):
    """Raised when an ``--until approval`` park is resumed with no decision.

    The wake gate is part of the parked state, so it is enforced where the
    mutation happens rather than only at the call site: a park recorded with
    :data:`WAKE_APPROVAL` refuses to append a resume row until the approval
    decision digest is supplied.
    """


#: Longest ``task_id`` that still fits the task journal's run-id budget.
#:
#: The park derives the journal run id as ``task_run_id(task_id)``, which is
#: ``"task-" + task_id``, and :mod:`bernstein.core.replay.journal` caps a run id
#: at 64 characters. This bound belongs here, at the boundary that actually has
#: the constraint, rather than in the shared approvals rule: an approval id from
#: the chat bridge or the pre-spawn gate never becomes a journal run id, so
#: tightening the shared rule to 59 would refuse ids no downstream sink objects
#: to. A task id between 60 and 64 characters is therefore approvable and
#: rejectable as normal; it simply cannot be durably parked, and says so with a
#: typed refusal instead of a bare ValueError from the journal.
_MAX_PARKABLE_TASK_ID_LEN = 64 - len("task-")


def validate_task_id(task_id: str) -> str:
    """Return ``task_id`` if it is safe for the park/resume path, else refuse.

    The shared approvals rule
    (:func:`~bernstein.core.orchestration.approval_gate.validate_approval_id`)
    plus the narrower journal run-id budget this path additionally needs.

    Raises:
        UnsafeTaskIdError: The identifier is empty, contains any character
            outside ``[A-Za-z0-9._-]``, does not start with an alphanumeric
            (which rules out ``.`` and ``..``), or is longer than
            :data:`_MAX_PARKABLE_TASK_ID_LEN`.
    """
    validate_approval_id(task_id)
    if len(task_id) > _MAX_PARKABLE_TASK_ID_LEN:
        msg = (
            f"refusing to park task id {task_id!r}: {len(task_id)} characters exceeds the "
            f"{_MAX_PARKABLE_TASK_ID_LEN}-character journal run-id budget"
        )
        raise UnsafeTaskIdError(msg)
    return task_id


def _contained_approval_path(workdir: Path, task_id: str, suffix: str) -> Path:
    """Return the contained ``<approvals>/<task_id><suffix>`` path.

    Delegates to :func:`~bernstein.core.orchestration.approval_gate.approval_path`
    so this module and the CLI decision commands cannot disagree about either
    the identifier rule or the containment check.

    Raises:
        UnsafeTaskIdError: The identifier is unsafe, or the resolved path
            escapes the approvals directory.
    """
    return approval_path(workdir, task_id, suffix)


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
# Receipt identity (the receipt must bind the park it is used for)
# ---------------------------------------------------------------------------


def _as_index(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def find_suspension_receipt(
    *,
    chain: AuditChainStore,
    task_id: str,
    suspend_row: SuspendRow,
) -> AuditEvent | None:
    """Return the ``task.suspend_receipt`` bound to exactly ``suspend_row``.

    Selection is by identity, not recency: the receipt must name ``task_id``
    and bind the row's ``event_hash`` and journal index. A task parked more
    than once therefore resolves to the receipt for the row being resumed
    rather than to whichever receipt happens to be last on the chain.

    Returns:
        The matching :class:`AuditEvent`, or ``None`` when the chain holds no
        receipt for this row.
    """
    from bernstein.core.security.audit_chain import EVENT_TASK_SUSPENDED

    for event in reversed(chain.query(event_type=EVENT_TASK_SUSPENDED)):
        details = event.details
        if str(details.get("task_id", "")) != task_id:
            continue
        if str(details.get("suspend_event_hash", "")) != suspend_row.event_hash:
            continue
        if _as_index(details.get("journal_index")) != suspend_row.journal_index:
            continue
        return event
    return None


@dataclass(frozen=True)
class Settlement:
    """One record claiming that a park was settled.

    A settlement is recorded twice, in two independent stores: the HMAC audit
    chain (``task.resume_receipt``) and the task's own event journal
    (``task.resume`` row). Both are represented here so the mutation guard and
    the offline proof can share one definition instead of each inventing its
    own.

    Attributes:
        source: ``"chain"`` or ``"journal"``.
        identifier: The receipt HMAC, or the journal row's ``event_hash``.
        binds_receipt: Whether it references the park's suspend receipt hash.
        binds_row: Whether it references the park's suspend row hash.
    """

    source: str
    identifier: str
    binds_receipt: bool
    binds_row: bool

    @property
    def consistent(self) -> bool:
        """Whether it references *both* identifiers of the same park."""
        return self.binds_receipt and self.binds_row


def find_settlements(
    *,
    sdd_dir: Path,
    task_id: str,
    chain: AuditChainStore,
    suspend_receipt_hash: str,
    suspend_event_hash: str,
) -> list[Settlement]:
    """Return every record claiming to settle this park, from **both** stores.

    This is the single definition of "settled" that the resume guard and the
    continuity proof both use. Two properties matter:

    * **Both stores are consulted.** A settlement is written to the audit chain
      *and* to the task journal. HMAC chaining only detects modification or
      removal of a non-terminal entry, so dropping the last line of the audit
      file leaves ``chain.verify()`` returning ``True`` while erasing the
      receipt. The journal still holds the resume row, so the union of the two
      stores is what an attacker would have to defeat, not either one alone.
    * **A claim is either identifier, not both.** A record that references the
      park's suspend receipt *or* its suspend row is claiming this park. A
      record matching only one is inconsistent evidence: it must not be
      silently ignored by the proof while the guard treats it as settled, which
      is exactly how the two paths drifted apart before.

    Args:
        sdd_dir: Project ``.sdd`` directory (for the task journal).
        task_id: The parked task.
        chain: Audit chain store holding the receipts.
        suspend_receipt_hash: HMAC of the park's suspend receipt.
        suspend_event_hash: Merkle hash of the park's suspend row.

    Returns:
        Every claiming record, in chain-then-journal order.
    """
    from bernstein.core.security.audit_chain import EVENT_TASK_RESUMED

    found: list[Settlement] = []

    for event in chain.query(event_type=EVENT_TASK_RESUMED):
        details = event.details
        if str(details.get("task_id", "")) != task_id:
            continue
        binds_receipt = (
            bool(suspend_receipt_hash) and str(details.get("suspend_receipt_hash", "")) == suspend_receipt_hash
        )
        binds_row = bool(suspend_event_hash) and str(details.get("suspend_event_hash", "")) == suspend_event_hash
        if binds_receipt or binds_row:
            found.append(
                Settlement(
                    source="chain",
                    identifier=event.hmac,
                    binds_receipt=binds_receipt,
                    binds_row=binds_row,
                )
            )

    for row in _journal_rows(sdd_dir, task_id):
        if row.get("event") != JOURNAL_EVENT_RESUME:
            continue
        if str(row.get("task_id", "")) != task_id:
            continue
        binds_receipt = bool(suspend_receipt_hash) and str(row.get("suspend_receipt_hash", "")) == suspend_receipt_hash
        binds_row = bool(suspend_event_hash) and str(row.get("continued_from_event_hash", "")) == suspend_event_hash
        if binds_receipt or binds_row:
            found.append(
                Settlement(
                    source="journal",
                    identifier=str(row.get("event_hash", "")),
                    binds_receipt=binds_receipt,
                    binds_row=binds_row,
                )
            )

    return found


def verify_suspension_receipt(
    *,
    chain: AuditChainStore,
    task_id: str,
    suspend_receipt_hash: str,
    suspend_event_hash: str = "",
    journal_index: int | None = None,
) -> AuditEvent:
    """Return the suspend receipt for ``suspend_receipt_hash``, or refuse.

    The identity check every release and every resume runs *before* it mutates
    anything: the hash must resolve to a real ``task.suspend_receipt`` on the
    HMAC chain, that receipt must belong to ``task_id``, and -- when the caller
    supplies them -- it must bind the given suspend row hash and journal index.

    Args:
        chain: The audit chain store holding the receipt.
        task_id: The task the caller is acting on.
        suspend_receipt_hash: HMAC of the receipt being claimed.
        suspend_event_hash: Optional suspend row hash the receipt must bind.
        journal_index: Optional journal index the receipt must bind.

    Returns:
        The validated :class:`AuditEvent`.

    Raises:
        ReleaseWithoutReceiptError: ``suspend_receipt_hash`` is empty.
        SuspendReceiptMismatchError: The hash names no receipt on the chain, or
            the receipt it names binds a different task or a different row.
    """
    from bernstein.core.security.audit_chain import EVENT_TASK_SUSPENDED

    if not suspend_receipt_hash:
        raise ReleaseWithoutReceiptError(f"refusing to act on task {task_id!r}: no suspend receipt (fail closed)")

    matches = [e for e in chain.query(event_type=EVENT_TASK_SUSPENDED) if e.hmac == suspend_receipt_hash]
    if not matches:
        msg = (
            f"refusing to act on task {task_id!r}: no task.suspend_receipt on the chain "
            f"with hmac {suspend_receipt_hash[:16]}..."
        )
        raise SuspendReceiptMismatchError(msg)

    receipt = matches[-1]
    receipt_task = str(receipt.details.get("task_id", ""))
    if receipt_task != task_id:
        msg = (
            f"refusing to act on task {task_id!r}: suspend receipt "
            f"{suspend_receipt_hash[:16]}... belongs to task {receipt_task!r}"
        )
        raise SuspendReceiptMismatchError(msg)

    if suspend_event_hash:
        bound = str(receipt.details.get("suspend_event_hash", ""))
        if bound != suspend_event_hash:
            msg = (
                f"refusing to act on task {task_id!r}: suspend receipt "
                f"{suspend_receipt_hash[:16]}... binds suspend row {bound[:16]}..., "
                f"not the selected row {suspend_event_hash[:16]}..."
            )
            raise SuspendReceiptMismatchError(msg)

    if journal_index is not None:
        bound_index = _as_index(receipt.details.get("journal_index"))
        if bound_index != journal_index:
            msg = (
                f"refusing to act on task {task_id!r}: suspend receipt "
                f"{suspend_receipt_hash[:16]}... binds journal index {bound_index}, "
                f"not the selected index {journal_index}"
            )
            raise SuspendReceiptMismatchError(msg)

    return receipt


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
    suspend_event_hash: str = "",
) -> ReleaseResult:
    """Release the seat, sandbox, process, and envelope headroom for a park.

    Every release hangs off ``suspend_receipt_hash``; the receipt's *identity*
    is validated *before any physical effect runs*, so a release with no
    matching receipt -- absent, or belonging to a different task or a different
    suspend row -- is rejected and no seat, sandbox, or process is touched. Each
    release appends a ``task.suspend_resource_release`` row to the audit chain
    referencing the receipt, and the budget release additionally emits a chained
    budget event.

    Args:
        chain: The audit chain store accepting the release rows.
        task_id: The parked task.
        suspend_receipt_hash: HMAC of the ``task.suspend_receipt`` (never
            empty).
        envelope: Envelope whose headroom is released.
        reserved_usd: Reservation held at park time.
        spent_usd: Spend recorded against the reservation at park time.
        handles: Physical release effects; ``None`` releases only budget.
        suspend_event_hash: Optional suspend row hash the receipt must bind, so
            a park that ran twice cannot release against the wrong row.

    Raises:
        ReleaseWithoutReceiptError: ``suspend_receipt_hash`` is empty.
        SuspendReceiptMismatchError: The hash names no receipt on the chain, or
            the receipt it names binds a different task or suspend row.
    """
    if not suspend_receipt_hash:
        raise ReleaseWithoutReceiptError(
            f"refusing to release resources for task {task_id!r}: no suspend receipt (fail closed)"
        )
    # Receipt identity before effect: a non-empty hash is not evidence on its
    # own, so the receipt is resolved and checked against this park first.
    verify_suspension_receipt(
        chain=chain,
        task_id=task_id,
        suspend_receipt_hash=suspend_receipt_hash,
        suspend_event_hash=suspend_event_hash,
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

    Raises:
        UnsafeTaskIdError: ``task_id`` is not a safe identifier. Checked at the
            park boundary so a task can never be parked under an id the resume
            path would later refuse.
    """
    from bernstein.core.cost.budget_actions import compute_released_headroom
    from bernstein.core.persistence.work_ledger import KIND_TASK_SUSPENDED
    from bernstein.core.security.audit_chain import record_task_suspension

    validate_task_id(task_id)
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
        suspend_event_hash=suspend_row.event_hash,
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

    Raises:
        UnsafeTaskIdError: The row's ``task_id`` is not a safe identifier.
        ReleaseWithoutReceiptError: ``suspend_receipt_hash`` is empty.
        SuspendReceiptMismatchError: The receipt does not bind this suspend row.
        SuspensionAlreadySettledError: This park already carries a resume
            receipt. A park settles once, so one approval cannot be spent
            twice; park again for a fresh suspend receipt.
        ResumeApprovalRequiredError: The park is gated on approval and no
            approval decision digest was supplied.
    """
    from bernstein.core.persistence.work_ledger import KIND_TASK_RESUMED
    from bernstein.core.security.audit_chain import record_task_resume

    # Every precondition is checked before the journal is touched, so a refused
    # resume leaves the task's Merkle chain byte-identical to the parked state.
    validate_task_id(suspend_row.task_id)
    verify_suspension_receipt(
        chain=chain,
        task_id=suspend_row.task_id,
        suspend_receipt_hash=suspend_receipt_hash,
        suspend_event_hash=suspend_row.event_hash,
        journal_index=suspend_row.journal_index,
    )
    # A park settles once. Without this the approval gate below would be a
    # presence check that one decision file could satisfy repeatedly, so a
    # single operator approval would authorise an unbounded number of resumes.
    # The same definition the offline proof uses, over the same two stores, so
    # the two cannot disagree about whether this park is spent.
    settlements = find_settlements(
        sdd_dir=sdd_dir,
        task_id=suspend_row.task_id,
        chain=chain,
        suspend_receipt_hash=suspend_receipt_hash,
        suspend_event_hash=suspend_row.event_hash,
    )
    if settlements:
        sources = ", ".join(sorted({f"{s.source}:{s.identifier[:16]}" for s in settlements}))
        msg = (
            f"refusing to resume task {suspend_row.task_id!r}: this park was already settled "
            f"({sources}) (park again to obtain a fresh suspend receipt)"
        )
        raise SuspensionAlreadySettledError(msg)
    if suspend_row.wake_condition == WAKE_APPROVAL and not approval_ref:
        msg = (
            f"refusing to resume task {suspend_row.task_id!r}: parked until approval and "
            "no approval decision has landed (fail closed)"
        )
        raise ResumeApprovalRequiredError(msg)

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

    The decision file name is derived from ``task_id``, so the identifier is
    validated and the resolved path is confirmed to stay inside the approvals
    directory before anything is read.

    Raises:
        UnsafeTaskIdError: ``task_id`` is not a safe single path segment, or
            the derived path escapes the approvals directory.
    """
    approved = _contained_approval_path(workdir, task_id, ".approved")
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

    The marker name is derived from ``task_id``, so the identifier is validated
    and the resolved path is confirmed to stay inside the approvals directory
    before the directory is created or anything is written. This is an
    approvals sink, so it applies the shared approvals rule rather than the
    narrower parkable-id budget: the budget is a constraint of the journal run
    id, and a marker file is not one.

    Raises:
        UnsafeTaskIdError: ``task_id`` is not a safe single path segment, or
            the derived path escapes the approvals directory.
    """
    # Resolve (and therefore validate) before creating any directory.
    marker = _contained_approval_path(workdir, task_id, ".resumed")
    marker.parent.mkdir(parents=True, exist_ok=True)
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
        status: The machine-readable outcome, one of
            :data:`CONTINUITY_VERIFIED`, :data:`CONTINUITY_PENDING`, or
            :data:`CONTINUITY_FAILED`. Branch on this. ``verified`` means a
            settlement happened and its proof holds; ``pending`` means the park
            has not settled yet, so there is nothing to prove; ``failed`` means
            a settlement is claimed but its evidence does not hold.
        ok: ``True`` when no integrity failure was found, which covers both
            ``verified`` and ``pending``. This is deliberately *not* "a resume
            was verified" -- a live park is not a broken proof, and collapsing
            the two would make every parked task in a fleet sweep look like a
            failure. Test ``status == CONTINUITY_VERIFIED`` (or ``resumed``)
            when you need a settled, proven continuity.
        chain_ok: Whether the HMAC audit chain verified.
        journal_ok: Whether the task journal Merkle chain verified.
        resumed: Whether a resume receipt *bound to the parked suspend receipt*
            was found. A resume receipt for some other park does not count.
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
    status: str = CONTINUITY_FAILED

    @property
    def pending(self) -> bool:
        """Whether the park simply has not settled yet (nothing to prove)."""
        return self.status == CONTINUITY_PENDING

    @property
    def verified(self) -> bool:
        """Whether a settlement happened *and* its continuity proof holds."""
        return self.status == CONTINUITY_VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
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


def _journal_rows(sdd_dir: Path, task_id: str) -> list[dict[str, Any]]:
    path = _journal_path(sdd_dir, task_id)
    if not path.exists():
        return []
    return list(load_events(path))


def _row_present(rows: list[dict[str, Any]], event: str, task_id: str, event_hash: str) -> bool:
    """Return whether ``rows`` holds a row of ``event`` with that exact hash."""
    return any(
        row.get("event") == event
        and str(row.get("task_id", "")) == task_id
        and str(row.get("event_hash", "")) == event_hash
        for row in rows
    )


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
    3. a ``task.resume_receipt`` exists that hangs off the parked
       ``suspend_receipt`` *and* references the parked suspend row's identity,
       and the recorded continuation mode / workspace match / downgrade reason
       describe how it continued (warm from the parked hash, or a recorded fork
       or cold downgrade with its reason);
    4. both receipts name rows the task journal actually holds.

    The outcome is a tri-state on :attr:`ContinuityResult.status`:

    * ``verified`` -- a settlement happened and its proof holds.
    * ``pending`` -- the park has not settled yet, so there is nothing to
      prove. Not a failure: a live park is an incomplete lifecycle, and
      reporting it as broken would bury real breaks in a fleet sweep.
    * ``failed`` -- a settlement is claimed but its evidence does not hold: a
      resume receipt hanging off another park's suspend receipt, a receipt
      naming a suspend or resume row the journal does not hold, more than one
      settlement of a single park, or a broken chain or journal.

    The distinction between ``pending`` and ``failed`` is which suspend row a
    resume receipt *claims*, not merely whether any resume exists: a task
    parked twice with only the first park settled leaves the second park
    ``pending``, because those receipts claim a different row.

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
            status=CONTINUITY_FAILED,
        )

    journal_rows = _journal_rows(sdd_dir, task_id)

    # Scope is every park on the task, not just the latest. Scoping the proof
    # to suspend_events[-1] meant an ordinary "park again" -- the very
    # remediation documented for a spent park -- stopped the verifier looking
    # at earlier parks, so replay damage already on the chain self-laundered.
    # An attacker never has to defeat the check; they just add a park.
    for earlier in suspend_events:
        earlier_hash = str(earlier.details.get("suspend_event_hash", ""))
        earlier_settlements = find_settlements(
            sdd_dir=sdd_dir,
            task_id=task_id,
            chain=chain,
            suspend_receipt_hash=earlier.hmac,
            suspend_event_hash=earlier_hash,
        )
        chain_consistent = [s for s in earlier_settlements if s.source == "chain" and s.consistent]
        if len(chain_consistent) > 1:
            errors.append(
                f"park {earlier_hash[:16]}... was settled {len(chain_consistent)} times: "
                "a suspend receipt must carry exactly one resume receipt"
            )
        # A record that matches one identifier of a park but not the other is
        # inconsistent evidence about that park. The writer treats it as a
        # settlement (it will refuse to resume), so the proof must surface it
        # rather than reporting nothing at all.
        for inconsistent in (s for s in earlier_settlements if s.source == "chain" and not s.consistent):
            which = "suspend receipt" if inconsistent.binds_receipt else "suspend row"
            errors.append(
                f"resume receipt {inconsistent.identifier[:16]}... references the {which} of park "
                f"{earlier_hash[:16]}... but not its counterpart: the settlement record is inconsistent"
            )

    suspend_event = suspend_events[-1]
    parked_hash = str(suspend_event.details.get("suspend_event_hash", ""))
    if not parked_hash:
        errors.append(f"suspend receipt for task {task_id!r} binds no suspend row hash")

    # The receipt must reference a suspend row that actually exists in this
    # task's journal: a receipt naming a row no journal holds is unrelated
    # evidence, not a proof of this park.
    if parked_hash and not _row_present(journal_rows, JOURNAL_EVENT_SUSPEND, task_id, parked_hash):
        errors.append(
            f"suspend receipt references suspend row {parked_hash[:16]}... which is absent from the task journal"
        )

    # The reported lifecycle state describes the current (latest) park, using
    # the same settlement definition as the guard: a record consistent on both
    # identifiers of this park.
    bound_resumes = [
        e
        for e in resume_events
        if parked_hash
        and str(e.details.get("suspend_event_hash", "")) == parked_hash
        and str(e.details.get("suspend_receipt_hash", "")) == suspend_event.hmac
    ]

    resumed = bool(bound_resumes)
    effective_mode = ""
    workspace_match = False
    downgrade_reason = ""
    if bound_resumes:
        resume_event = bound_resumes[-1]
        effective_mode = str(resume_event.details.get("effective_mode", ""))
        workspace_match = bool(resume_event.details.get("workspace_match", False))
        downgrade_reason = str(resume_event.details.get("downgrade_reason", ""))
        resume_row_hash = str(resume_event.details.get("resume_event_hash", ""))
        if not resume_row_hash or not _row_present(journal_rows, JOURNAL_EVENT_RESUME, task_id, resume_row_hash):
            errors.append(
                f"resume receipt references resume row {resume_row_hash[:16]}... which is absent from the task journal"
            )
        # A warm continuation asserts it resumed the parked native session from
        # the parked workspace hash, so the hashes must have matched -- a warm
        # resume without a match is the one genuine inconsistency (a drift must
        # downgrade, never silently resume warm). A fork or cold continuation is
        # surfaced with its recorded reason from the receipt but is not itself a
        # failure: the AC is "warm from the parked hash, or a recorded fork/cold
        # downgrade with its reason".
        if effective_mode == str(RetryMode.WARM) and not workspace_match:
            errors.append("warm resume recorded without a workspace-hash match")

    # No failure found: the park is either settled and proven, or still live.
    # A live park keeps ok=True so a fleet sweep is not flooded with false
    # failures and so the CLI's exit code for the ordinary parked case is
    # unchanged; ``status`` is what tells the two apart.
    ok = chain_ok and journal_ok and not errors
    if not ok:
        status = CONTINUITY_FAILED
    elif resumed:
        status = CONTINUITY_VERIFIED
    else:
        status = CONTINUITY_PENDING
    return ContinuityResult(
        ok=ok,
        chain_ok=chain_ok,
        journal_ok=journal_ok,
        resumed=resumed,
        effective_mode=effective_mode,
        workspace_match=workspace_match,
        downgrade_reason=downgrade_reason,
        errors=errors,
        status=status,
    )


__all__ = [
    "CONTINUITY_FAILED",
    "CONTINUITY_PENDING",
    "CONTINUITY_VERIFIED",
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
    "ResumeApprovalRequiredError",
    "ResumeResult",
    "Settlement",
    "SuspendReceiptMismatchError",
    "SuspendRow",
    "SuspensionAlreadySettledError",
    "UnsafeTaskIdError",
    "approval_decision_ref",
    "decide_resume",
    "find_settlements",
    "find_suspension_receipt",
    "latest_suspension",
    "park_task",
    "record_task_suspension_row",
    "release_resources",
    "resume_task",
    "validate_task_id",
    "verify_suspension_continuity",
    "verify_suspension_receipt",
    "write_resume_marker",
]
