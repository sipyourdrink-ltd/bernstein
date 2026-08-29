"""Durable, expiring leases on hub task claims.

The hub hands a task to exactly one donor worker for a bounded time, and must
safely give it to somebody else when that donor disappears mid-task -- a laptop
sleeps, a process dies, a network drops.  Nothing in the codebase did that
before this module: :meth:`TaskStore.claim_next` hands a task over permanently
with no expiry, and ``POST /cluster/steal`` is admin-triggered load balancing
between trusted, pre-registered cluster nodes driven by reported queue depth.
Neither answers "a donor claimed this and went dark; give it to someone else
after N minutes, exactly once."

What a lease is, and what decides it
------------------------------------

A lease is a claim with an expiry attached, which is why this mirrors
:class:`~bernstein.core.tasks.task_store_core.TaskStore`'s concurrency model
rather than ``NodeRegistry``'s: the latter tracks *node* liveness and has no
concept of a lease on a unit of work.

The seam with :mod:`bernstein.core.volunteer.claim` is deliberate and worth
stating, because the two look adjacent.  That module decides whether a claim is
*legitimate* -- etiquette on a public issue thread, scoped by ``viewerDidAuthor``
against a staleness window.  This module decides where a claim *survives a
process death*.  Nothing here re-derives etiquette and nothing there knows how a
lease is persisted; the two clocks (``DEFAULT_CLAIM_STALENESS`` and a lease's
``ttl_seconds``) answer different questions and must not be collapsed into one.

Expiry is a comparison, not a state
-----------------------------------

``expires_at`` is stored; "expired" never is.  A lease is expired when
``clock() >= lease.expires_at``, evaluated under the lock at the moment somebody
asks.  Reaping is checked inline as the first statement of every mutating call
-- the same shape as the lazy-delete of stale heap entries in ``claim_next`` --
rather than in a background task.  A background reaper needs its own
start/stop lifecycle across the app's lifespan, and unless it takes the very
same lock it opens a real window between "the reaper decides lease X is expired"
and "claim() grants X to worker B" racing "heartbeat() for X from worker A, who
was slow rather than dead."  Taking the same lock buys nothing over checking
inline, so this checks inline and the race cannot be expressed.

A reap appends a record, and that is not an optimisation
--------------------------------------------------------

The JSONL log is append-only and replayed last-write-wins, so state after a
restart is whatever the log says.  If reaping only dropped the in-memory lease,
a restart would replay the original claim, observe it expired *again*, and
reassign the task a second time: "exactly once per expiry" would hold within one
process and quietly break across a restart, which is the case the hub exists to
handle.  So a reassignment is itself a durable event with its own record.

Single-process, like the store it mirrors
-----------------------------------------

Mutations are coordinated by an in-process :class:`asyncio.Lock` and the append
path takes no OS-level file lock (no ``fcntl.flock``).  This store is therefore
**single-process only**, exactly as ``TaskStore`` is.  Running the hub under
``uvicorn --workers N>1`` or several replicas would let two processes each
believe they hold the only copy of a lease and double-assign a task.  The hub's
serve command must refuse to start with more than one worker process, the way
:func:`bernstein.core.server.server_app.preflight_multi_worker_guard` already
does for the main server.  Lifting this is out of scope here and for #3877 as a
whole.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from bernstein.core.security.audit_dsse import export_public_key_pem, keyid_from_public_key
from bernstein.core.volunteer.budget import (
    DEFAULT_LEDGER_PATH,
    BudgetClaimError,
    VolunteerBudget,
    complete_claim,
    load_ledger,
    reserve_claim,
    save_ledger,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

    from bernstein.adapters.capability_profile import AdapterCapabilityProfile

logger = logging.getLogger(__name__)

#: Schema version stamped into every JSONL record, so a later reader can tell a
#: format change from a corrupt line.
LEASE_STORE_SCHEMA_VERSION: int = 1

#: Default location for a donor's persistent worker key, relative to the project
#: root.  Deliberately *not* the install key's path: that one names the CLI
#: install, this one names a volunteer worker enrollment, and a collision on one
#: file would make the two identities indistinguishable.
DEFAULT_WORKER_KEY_PATH: str = ".sdd/runtime/volunteer/worker.key"

#: Raw byte length of an Ed25519 private seed.
_SEED_BYTES: int = 32


class LeaseStoreError(RuntimeError):
    """Raised when a worker key cannot be read or is malformed."""


class LeaseRefusalReason(Enum):
    """Why a lease operation was refused.

    Stable string values: they are the vocabulary an HTTP surface projects to a
    donor, and a verifier comparing refusals across a fleet should see one set
    of codes rather than each caller's paraphrase.
    """

    ALREADY_LEASED = "already_leased"
    ALREADY_SUBMITTED = "already_submitted"
    NOT_LEASE_HOLDER = "not_lease_holder"
    LEASE_REASSIGNED = "lease_reassigned"
    NO_LEASE = "no_lease"
    UNKNOWN_WORKER = "unknown_worker"
    TASK_BUDGET_EXHAUSTED = "task_budget_exhausted"
    WALL_CLOCK_BUDGET_EXHAUSTED = "wall_clock_budget_exhausted"
    TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"
    SIZE_CAP_EXCEEDED = "size_cap_exceeded"
    TASK_SIZE_UNKNOWN = "task_size_unknown"
    LOCAL_ONLY_ADAPTER_REQUIRED = "local_only_adapter_required"


@dataclass(frozen=True, slots=True)
class LeaseRefusal:
    """A refused lease operation, as a value rather than an exception.

    Refusals are the ordinary outcome here -- two donors reaching for one task is
    the case this store exists to arbitrate -- and the volunteer package's
    discipline is that an ordinary outcome arriving as an exception is one
    somebody has to guess how to catch.  ``TaskStore`` raises; this does not.

    Attributes:
        reason: The stable reason code.
        detail: Human-readable explanation naming the task and worker.
    """

    reason: LeaseRefusalReason
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """The refusal as a record."""
        return {"reason": self.reason.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Submission:
    """What a worker handed back for a leased task.

    The store deals in opaque strings on purpose: wiring real protocol documents
    through is downstream work (#3883), and a lease does not need to understand a
    receipt bundle to record that one arrived.

    Attributes:
        bundle_digest: Content address of the result bundle.
        location: Where the bundle can be fetched from.
        submitted_at: Unix timestamp of the submission.
    """

    bundle_digest: str
    location: str
    submitted_at: float

    def to_dict(self) -> dict[str, Any]:
        """The submission as a record."""
        return {
            "bundle_digest": self.bundle_digest,
            "location": self.location,
            "submitted_at": self.submitted_at,
        }


@dataclass(frozen=True, slots=True)
class Lease:
    """One task held by one worker until a deadline.

    Frozen, like every other volunteer dataclass: a mutation produces a new
    instance via :func:`dataclasses.replace` and appends a fresh snapshot, so the
    in-memory map is a projection of the log rather than a second source of
    truth.

    Attributes:
        task_id: The leased task.
        worker_id: The holder, as :func:`keyid_from_public_key` derives it.
        claimed_at: When the current generation was granted.
        expires_at: Unix timestamp after which the lease is expired.  Wall clock
            rather than :func:`time.monotonic`, because a lease has to survive a
            process restart and a monotonic reading does not.
        heartbeat_at: When the expiry was last extended; equal to
            :attr:`claimed_at` until the first heartbeat.
        generation: 1 on the first claim, incremented on each reassignment, so a
            worker can tell "still mine" from "someone else's now".
        ttl_seconds: The window the holder was granted.  Stored so a heartbeat
            extends by the same amount the claim asked for without the caller
            having to repeat it.
        submission: The result handed back, or ``None`` while work is in flight.
    """

    task_id: str
    worker_id: str
    claimed_at: float
    expires_at: float
    heartbeat_at: float
    generation: int
    ttl_seconds: int
    submission: Submission | None = None

    def is_expired(self, now: float) -> bool:
        """Whether the lease has passed its deadline at ``now``."""
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """The lease as a record."""
        return {
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "claimed_at": self.claimed_at,
            "expires_at": self.expires_at,
            "heartbeat_at": self.heartbeat_at,
            "generation": self.generation,
            "ttl_seconds": self.ttl_seconds,
            "submission": self.submission.to_dict() if self.submission is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ReassignedLease:
    """A lease that expired unrenewed and was taken back.

    Attributes:
        task_id: The task that became claimable again.
        worker_id: The worker that lost it.
        generation: Generation of the lease that expired.
        expires_at: The deadline that passed.
        reaped_at: When the store observed the expiry.
    """

    task_id: str
    worker_id: str
    generation: int
    expires_at: float
    reaped_at: float

    def to_dict(self) -> dict[str, Any]:
        """The reassignment as a record."""
        return {
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "generation": self.generation,
            "expires_at": self.expires_at,
            "reaped_at": self.reaped_at,
        }


def load_or_create_worker_key(path: Path) -> Ed25519PrivateKey:
    """Load or generate the donor's persistent worker key at ``path``.

    A worker's Ed25519 identity has to be the *same* key across many claims: the
    result receipt bundle's chain links successive bundles by the previous
    bundle's digest, all signed by one key, so a fresh keypair per claim would
    break continuity for anyone verifying the sequence.

    Follows the shape of
    :func:`bernstein.core.security.install_key.load_or_create_install_key`
    without calling it -- that key names the CLI install, this one names a
    volunteer worker enrollment, and the two must not collide on one file.

    Args:
        path: Where the raw 32-byte seed lives.

    Returns:
        The loaded or freshly generated private key.

    Raises:
        LeaseStoreError: The file exists but cannot be read, or is not exactly
            32 raw bytes.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if path.exists():
        try:
            # Read the seed exactly as written: the create path below persists the
            # raw 32-byte private seed with no trailing delimiter, so the bytes on
            # disk ARE the key.  Do NOT strip(): a random Ed25519 seed starts or
            # ends with an ASCII-whitespace byte (0x09, 0x0a, 0x0b, 0x0c, 0x0d,
            # 0x20) ~4.7% of the time, and strip() would silently drop it,
            # corrupting a valid key into a "not 32 raw bytes" error.
            raw = path.read_bytes()
        except OSError as exc:
            raise LeaseStoreError(f"cannot read worker key {path}: {exc}") from exc
        if len(raw) != _SEED_BYTES:
            raise LeaseStoreError(f"worker key {path} is not {_SEED_BYTES} raw bytes; refusing to use it")
        return Ed25519PrivateKey.from_private_bytes(raw)

    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    private_key = Ed25519PrivateKey.generate()
    # O_EXCL, not a plain open: it is what stops two concurrent first runs from
    # both "creating" the key and clobbering each other's seed.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, private_key.private_bytes_raw())
    finally:
        os.close(fd)
    path.chmod(0o600)
    return private_key


class LeaseStore:
    """Leases on hub tasks, held in memory and durable in a JSONL log.

    Single-process only; see the module docstring for why, and for what an
    operator must do about it.

    Every mutating method takes the same :class:`asyncio.Lock` and reaps expired
    leases as its first statement inside it, which is what makes "reassigns an
    abandoned task exactly once per expiry" a property of the lock rather than of
    timing.  Every method is ``async def`` even where nothing is awaited, so the
    call sites stay uniform once a FastAPI surface is the first caller.
    """

    def __init__(
        self,
        jsonl_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        budget: VolunteerBudget | None = None,
        budget_ledger_path: Path = DEFAULT_LEDGER_PATH,
    ) -> None:
        """Build a store over ``jsonl_path``, replaying it if it exists.

        Args:
            jsonl_path: The append-only log.  Created on first write.
            clock: Source of the current Unix timestamp.  Injected so expiry
                tests are exact rather than timing-dependent.
            budget: Optional donor policy enforced before a lease is granted.
            budget_ledger_path: Durable ledger used when ``budget`` is set.
        """
        self._jsonl_path = jsonl_path
        self._clock = clock
        self._budget = budget
        self._budget_ledger_path = budget_ledger_path
        self._lock = asyncio.Lock()
        self._leases: dict[str, Lease] = {}
        self._workers: dict[str, str] = {}
        # (task_id, worker_id) -> generation the worker lost.  Kept so a worker
        # coming back after its lease was reaped is told *that* ("reassigned")
        # rather than the indistinguishable "you are not the holder".
        self._reassigned: dict[tuple[str, str], int] = {}
        # task_id -> highest generation ever granted on it.  Kept separately from
        # ``_leases`` because a lease is *removed* when it is released or reaped,
        # and the counter has to outlive it: if it did not, a task claimed,
        # released, and claimed again would hand out generation 1 twice, and a
        # worker holding the first could not tell it had been superseded.
        self._last_generation: dict[str, int] = {}
        self._replay()

    # -- reads ------------------------------------------------------------

    def lease_for(self, task_id: str) -> Lease | None:
        """The current lease on ``task_id``, without reaping or locking.

        A read of the projection as it stands.  Callers deciding whether to act
        must go through a mutating method, which reaps first; this is for
        inspection and for tests.
        """
        return self._leases.get(task_id)

    def is_enrolled(self, worker_id: str) -> bool:
        """Whether ``worker_id`` has been enrolled."""
        return worker_id in self._workers

    # -- mutations --------------------------------------------------------

    async def enroll(self, worker_pubkey: Ed25519PublicKey) -> str:
        """Register a worker and return its id.

        Idempotent by construction rather than by a special case:
        :func:`keyid_from_public_key` is a pure function of the key, so enrolling
        the same public key twice derives the same id and cannot produce two
        identities for one worker.

        Args:
            worker_pubkey: The worker's long-lived Ed25519 public key.

        Returns:
            The worker id, as the result bundle's ``worker_keyid`` carries it.
        """
        worker_id = keyid_from_public_key(worker_pubkey)
        async with self._lock:
            if worker_id in self._workers:
                return worker_id
            pem = export_public_key_pem(worker_pubkey).decode("ascii")
            self._workers[worker_id] = pem
            self._append(
                {
                    "kind": "worker",
                    "worker_id": worker_id,
                    "public_key_pem": pem,
                    "enrolled_at": self._clock(),
                }
            )
        return worker_id

    async def claim(
        self,
        task_id: str,
        worker_id: str,
        ttl_seconds: int,
        *,
        task_size: str = "s",
        token_estimate: int = 0,
        wall_clock_hours: float | None = None,
        adapter_profile: AdapterCapabilityProfile | None = None,
    ) -> Lease | LeaseRefusal:
        """Lease ``task_id`` to ``worker_id`` for ``ttl_seconds``.

        Args:
            task_id: The task to lease.
            worker_id: An enrolled worker.
            ttl_seconds: How long the lease is honoured before it may be reaped.
            task_size: Canonical size label used by donor admission.
            token_estimate: Tokens reserved before the external claim.
            wall_clock_hours: Expected runtime; defaults to the lease TTL.
            adapter_profile: Registered capability profile selected for the run.

        Returns:
            The granted :class:`Lease`, or a :class:`LeaseRefusal` when the task
            is already leased to somebody else, already submitted, or the worker
            was never enrolled.
        """
        async with self._lock:
            self._reap_expired_unlocked()
            if worker_id not in self._workers:
                return LeaseRefusal(
                    LeaseRefusalReason.UNKNOWN_WORKER,
                    f"worker {worker_id} is not enrolled",
                )
            now = self._clock()
            existing = self._leases.get(task_id)
            if existing is not None:
                if existing.submission is not None:
                    return LeaseRefusal(
                        LeaseRefusalReason.ALREADY_SUBMITTED,
                        f"task {task_id} already carries a submission from worker {existing.worker_id}",
                    )
                # A worker whose lease was reaped: distinguish "you were too slow"
                # (LEASE_REASSIGNED) from "someone else holds it now"
                # (ALREADY_LEASED).  Same check as _holder_refusal.
                if (task_id, worker_id) in self._reassigned:
                    return LeaseRefusal(
                        LeaseRefusalReason.LEASE_REASSIGNED,
                        f"lease on task {task_id} was taken back from worker {worker_id}",
                    )
                if existing.worker_id != worker_id:
                    # A worker whose own lease was reaped learns that here, not
                    # from a generic "already leased": the reason must match
                    # what heartbeat/submit report through _holder_refusal so
                    # a caller can apply one grace policy to all three.
                    if self._reassigned.get((task_id, worker_id)) is not None:
                        return LeaseRefusal(
                            LeaseRefusalReason.LEASE_REASSIGNED,
                            f"lease on task {task_id} was taken back from worker {worker_id} "
                            f"and is now held by worker {existing.worker_id}",
                        )
                    return LeaseRefusal(
                        LeaseRefusalReason.ALREADY_LEASED,
                        f"task {task_id} is leased to worker {existing.worker_id} until {existing.expires_at}",
                    )
                # The holder re-claiming its own live lease extends it rather
                # than being refused: a worker that restarted mid-task and is
                # picking up where it left off is not a competitor for the task.
                renewed = replace(
                    existing,
                    expires_at=now + ttl_seconds,
                    heartbeat_at=now,
                    ttl_seconds=ttl_seconds,
                )
                return self._store_lease(renewed)
            # A worker whose lease was reaped but nobody else has claimed yet.
            if (task_id, worker_id) in self._reassigned:
                return LeaseRefusal(
                    LeaseRefusalReason.LEASE_REASSIGNED,
                    f"lease on task {task_id} was taken back from worker {worker_id}",
                )
            budget_refusal = self._reserve_budget_unlocked(
                task_id,
                task_size=task_size,
                token_estimate=token_estimate,
                wall_clock_hours=ttl_seconds / 3600 if wall_clock_hours is None else wall_clock_hours,
                adapter_profile=adapter_profile,
            )
            if budget_refusal is not None:
                return budget_refusal
            # Generation counts *holds* of this task, whoever held them and
            # however each ended, so it is strictly increasing per task and a
            # worker can always tell a lease of its own from a later one.
            prior = self._last_generation.get(task_id, 0)
            lease = Lease(
                task_id=task_id,
                worker_id=worker_id,
                claimed_at=now,
                expires_at=now + ttl_seconds,
                heartbeat_at=now,
                generation=prior + 1,
                ttl_seconds=ttl_seconds,
            )
            return self._store_lease(lease)

    async def heartbeat(self, task_id: str, worker_id: str) -> Lease | LeaseRefusal:
        """Extend the holder's lease by the window its claim asked for.

        Args:
            task_id: The leased task.
            worker_id: The worker claiming to hold it.

        Returns:
            The extended :class:`Lease`, or a :class:`LeaseRefusal`.
        """
        async with self._lock:
            self._reap_expired_unlocked()
            refusal = self._holder_refusal(task_id, worker_id)
            if refusal is not None:
                return refusal
            lease = self._leases[task_id]
            now = self._clock()
            # Only the deadline and the heartbeat move: claimed_at, generation
            # and ttl_seconds are what a caller uses to tell one hold from the
            # next, and resetting them would erase that.
            return self._store_lease(replace(lease, expires_at=now + lease.ttl_seconds, heartbeat_at=now))

    async def release(
        self,
        task_id: str,
        worker_id: str,
        *,
        actual_tokens: int | None = None,
    ) -> LeaseRefusal | None:
        """Give up a lease early, making the task immediately claimable.

        A voluntary release is not a reassignment: the worker is not recorded as
        having lost the lease, so a later heartbeat from it is answered with
        "there is no lease" rather than "yours was taken away".

        Args:
            task_id: The leased task.
            worker_id: The worker giving it up.

        Returns:
            ``None`` on success, or a :class:`LeaseRefusal`.
        """
        async with self._lock:
            self._reap_expired_unlocked()
            refusal = self._holder_refusal(task_id, worker_id)
            if refusal is not None:
                return refusal
            lease = self._leases[task_id]
            self._complete_budget_unlocked(lease, actual_tokens=actual_tokens)
            del self._leases[task_id]
            self._append({"kind": "release", "task_id": task_id, "generation": lease.generation})
        return None

    async def submit(
        self,
        task_id: str,
        worker_id: str,
        bundle_digest: str,
        location: str,
        *,
        actual_tokens: int | None = None,
    ) -> Lease | LeaseRefusal:
        """Record the result for a leased task.

        A second submission against a lease that already carries one is refused;
        that is the duplicate-submission criterion this store exists to enforce.

        Note that ``submit`` reaps first, like every other mutating call, so a
        worker whose lease lapsed while it was still running gates is answered
        with :attr:`LeaseRefusalReason.LEASE_REASSIGNED` -- distinct from
        :attr:`~LeaseRefusalReason.NOT_LEASE_HOLDER` precisely so a caller can
        tell "you were too slow" from "this was never yours" and apply its own
        grace policy.  This module holds no grace window of its own.

        Args:
            task_id: The leased task.
            worker_id: The worker submitting.
            bundle_digest: Content address of the result bundle.
            location: Where the bundle can be fetched from.

        Returns:
            The :class:`Lease` carrying the submission, or a
            :class:`LeaseRefusal`.
        """
        async with self._lock:
            self._reap_expired_unlocked()
            refusal = self._holder_refusal(task_id, worker_id)
            if refusal is not None:
                return refusal
            lease = self._leases[task_id]
            if lease.submission is not None:
                return LeaseRefusal(
                    LeaseRefusalReason.ALREADY_SUBMITTED,
                    f"task {task_id} already carries a submission from worker {lease.worker_id}",
                )
            submission = Submission(
                bundle_digest=bundle_digest,
                location=location,
                submitted_at=self._clock(),
            )
            self._complete_budget_unlocked(lease, actual_tokens=actual_tokens)
            return self._store_lease(replace(lease, submission=submission))

    async def reap_expired(self) -> tuple[ReassignedLease, ...]:
        """Take back every lease whose deadline has passed unrenewed.

        Called inline by every mutating method; exposed as well so an operator
        surface can drive it without pretending to claim something.

        Returns:
            The reassignments, ordered by task id so a caller comparing two runs
            sees a stable sequence.
        """
        async with self._lock:
            return self._reap_expired_unlocked()

    # -- internals --------------------------------------------------------

    def _holder_refusal(self, task_id: str, worker_id: str) -> LeaseRefusal | None:
        """Why ``worker_id`` may not act on ``task_id``, or ``None`` if it may.

        Distinguishes "your lease was reaped" from "this was never yours": the
        first is a worker that did hold the task and ran out of time, and telling
        the two apart is what lets a caller decide whether a late arrival
        deserves a grace window.
        """
        lease = self._leases.get(task_id)
        lost_generation = self._reassigned.get((task_id, worker_id))
        if lease is None:
            if lost_generation is not None:
                return LeaseRefusal(
                    LeaseRefusalReason.LEASE_REASSIGNED,
                    f"lease on task {task_id} held by worker {worker_id} expired and was taken back",
                )
            return LeaseRefusal(LeaseRefusalReason.NO_LEASE, f"task {task_id} is not leased")
        if lease.worker_id != worker_id:
            if lost_generation is not None:
                return LeaseRefusal(
                    LeaseRefusalReason.LEASE_REASSIGNED,
                    f"lease on task {task_id} was taken back from worker {worker_id} "
                    f"and is now held by worker {lease.worker_id}",
                )
            return LeaseRefusal(
                LeaseRefusalReason.NOT_LEASE_HOLDER,
                f"task {task_id} is leased to worker {lease.worker_id}, not {worker_id}",
            )
        return None

    def _reap_expired_unlocked(self) -> tuple[ReassignedLease, ...]:
        """Reap expired leases.  Caller must hold the lock.

        A lease carrying a submission is never reaped: the work arrived, so the
        task is finished rather than abandoned, and handing it to a second worker
        would duplicate completed effort.
        """
        now = self._clock()
        expired = sorted(
            (lease for lease in self._leases.values() if lease.submission is None and lease.is_expired(now)),
            key=lambda lease: lease.task_id,
        )
        reassigned: list[ReassignedLease] = []
        for lease in expired:
            record = ReassignedLease(
                task_id=lease.task_id,
                worker_id=lease.worker_id,
                generation=lease.generation,
                expires_at=lease.expires_at,
                reaped_at=now,
            )
            self._complete_budget_unlocked(lease, actual_tokens=None)
            del self._leases[lease.task_id]
            self._reassigned[lease.task_id, lease.worker_id] = lease.generation
            self._append({"kind": "reassign", **record.to_dict()})
            reassigned.append(record)
        return tuple(reassigned)

    def _reserve_budget_unlocked(
        self,
        task_id: str,
        *,
        task_size: str,
        token_estimate: int,
        wall_clock_hours: float,
        adapter_profile: AdapterCapabilityProfile | None,
    ) -> LeaseRefusal | None:
        """Reserve donor capacity before the lease event becomes durable."""
        if self._budget is None:
            return None
        ledger = load_ledger(self._budget_ledger_path)
        try:
            reserved = reserve_claim(
                self._budget,
                ledger,
                claim_id=task_id,
                task_size=task_size,
                token_estimate=token_estimate,
                wall_clock_hours=wall_clock_hours,
                adapter_profile=adapter_profile,
            )
        except BudgetClaimError as error:
            return LeaseRefusal(LeaseRefusalReason(error.refusal.reason), error.refusal.detail)
        save_ledger(reserved, self._budget_ledger_path)
        return None

    def _complete_budget_unlocked(self, lease: Lease, *, actual_tokens: int | None) -> None:
        """Reconcile terminal work while already holding the store lock."""
        if self._budget is None:
            return
        ledger = load_ledger(self._budget_ledger_path)
        reservation = next((item for item in ledger.reservations if item.claim_id == lease.task_id), None)
        if reservation is None:
            return
        elapsed_hours = max(0.0, self._clock() - lease.claimed_at) / 3600
        tokens = reservation.token_estimate if actual_tokens is None else actual_tokens
        save_ledger(
            complete_claim(ledger, claim_id=lease.task_id, hours=elapsed_hours, actual_tokens=tokens),
            self._budget_ledger_path,
        )

    def _store_lease(self, lease: Lease) -> Lease:
        """Put ``lease`` in the projection and append its snapshot."""
        self._track_lease(lease)
        self._append({"kind": "lease", **lease.to_dict()})
        return lease

    def _track_lease(self, lease: Lease) -> None:
        """Record ``lease`` in the projection, live map and generation counter.

        Shared by the write path and by replay so a rebuilt store carries the
        same generation counter a live one does; the counter is what a released
        or reaped task's next claim is numbered from.
        """
        self._leases[lease.task_id] = lease
        self._last_generation[lease.task_id] = max(
            self._last_generation.get(lease.task_id, 0),
            lease.generation,
        )

    def _append(self, record: dict[str, Any]) -> None:
        """Append one record to the log, flushed immediately.

        Flushed per record rather than buffered: a lease the store believes it
        granted but has not written is exactly the state a crash turns into a
        double-assignment.

        Serialised with sorted keys and no insignificant whitespace, matching
        :func:`bernstein.core.volunteer.manifest.canonical_manifest_bytes`, so
        two stores writing the same state produce the same bytes.
        """
        payload = {"schema_version": LEASE_STORE_SCHEMA_VERSION, **record}
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self._jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

    def _replay(self) -> None:
        """Rebuild the projection from the log, last write wins.

        A malformed line is logged and skipped rather than fatal, matching
        ``TaskStore.replay_jsonl``: one torn record at the tail of a crashed
        process should cost the last event, not the whole history.
        """
        if not self._jsonl_path.exists():
            return
        try:
            lines = self._jsonl_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LeaseStoreError(f"cannot read lease log {self._jsonl_path}: {exc}") from exc
        for number, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                record: dict[str, Any] = json.loads(line)
            except ValueError:
                logger.error("corrupted lease record at %s:%d - skipping", self._jsonl_path, number)
                continue
            if not isinstance(record, dict):
                continue
            self._apply_record(record)

    def _apply_record(self, record: dict[str, Any]) -> None:
        """Fold one replayed record into the projection."""
        kind = record.get("kind")
        if kind == "worker":
            worker_id = record.get("worker_id")
            if isinstance(worker_id, str):
                self._workers[worker_id] = str(record.get("public_key_pem", ""))
            return
        task_id = record.get("task_id")
        if not isinstance(task_id, str):
            return
        if kind == "lease":
            raw_submission = record.get("submission")
            submission = (
                Submission(
                    bundle_digest=str(raw_submission["bundle_digest"]),
                    location=str(raw_submission["location"]),
                    submitted_at=float(raw_submission["submitted_at"]),
                )
                if isinstance(raw_submission, dict)
                else None
            )
            self._track_lease(
                Lease(
                    task_id=task_id,
                    worker_id=str(record["worker_id"]),
                    claimed_at=float(record["claimed_at"]),
                    expires_at=float(record["expires_at"]),
                    heartbeat_at=float(record["heartbeat_at"]),
                    generation=int(record["generation"]),
                    ttl_seconds=int(record["ttl_seconds"]),
                    submission=submission,
                )
            )
        elif kind == "release":
            self._leases.pop(task_id, None)
        elif kind == "reassign":
            # The reassignment is why this replay cannot simply be "last lease
            # record wins": without it a restart would see the original claim,
            # find it expired, and reassign the task a second time.
            self._leases.pop(task_id, None)
            worker_id = record.get("worker_id")
            if isinstance(worker_id, str):
                self._reassigned[task_id, worker_id] = int(record.get("generation", 0))


__all__ = [
    "DEFAULT_WORKER_KEY_PATH",
    "LEASE_STORE_SCHEMA_VERSION",
    "Lease",
    "LeaseRefusal",
    "LeaseRefusalReason",
    "LeaseStore",
    "LeaseStoreError",
    "ReassignedLease",
    "Submission",
    "load_or_create_worker_key",
]
