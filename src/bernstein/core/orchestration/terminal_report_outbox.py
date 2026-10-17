"""Durable outbox for remote-worker terminal reports.

A worker node reaps its agent processes and must tell the central server the
terminal outcome. Posting that report once and dropping it on a 5xx, a 409, or
a dead socket leaves the task ``claimed`` forever with no live agent, and the
worker forgets it ever had an outcome to report. The outcome is therefore
written to this outbox *before* any delivery attempt, so it outlives both the
failed request and the worker process.

A record leaves the file only on an explicit acknowledgement from the server
(see :meth:`TerminalReportOutbox.settle`); retry/backoff state lives in the
file too, so a restart resumes the schedule instead of re-hammering an endpoint
that is already in trouble.

The shape follows the durable bulletin retry outbox (#2648) and reuses the
shared persistence primitives (``write_atomic_text``, ``cross_process_lock``).
JSONL over SQLite is deliberate: a worker's backlog is bounded by its own slot
count and an operator can read the file with ``cat``. Every mutation -- the
first record ever queued included -- is a locked load-modify-``write_atomic_text``
rather than an ``open(path, "a")``. An append carries no atomicity guarantee: a
crash mid-write leaves a fragment with no trailing newline, and the next append
concatenates onto that fragment, so one torn write destroys the report behind it
too. Appending also inherits the process umask where the rewrite pins 0o600, and
it follows a symlink planted at the runtime path instead of replacing it. A full
rewrite is free at this file's size and removes all three failure modes.

Concurrency: several workers may share one ``.sdd/`` directory, so every
mutation is a locked load-modify-save (the same discipline as
``FileLockManager._guard``). Records carry the node that produced them and only
that node drains them, so two nodes never double-post one report.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.atomic_write import write_atomic_text
from bernstein.core.persistence.file_locks import LockTimeout, cross_process_lock

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

#: The agent process exited 0 and its work reached the shared remote.
OUTCOME_DONE = "done"
#: The agent failed, or its work never left the node.
OUTCOME_FAILED = "failed"
#: The process is gone but its exit status is unknowable (already reaped by
#: someone else, or reaped without a waitable status). Never treated as
#: success: the caller reconciles against the server's authoritative state.
OUTCOME_UNKNOWN = "unknown"

#: Settlement dispositions -- why a record stopped being retried.
SETTLE_ACKED = "acked"
SETTLE_DUPLICATE = "duplicate"
SETTLE_CONFLICT = "conflict"
SETTLE_RESOLVED = "resolved"
SETTLE_GONE = "gone"
#: The central refused the report because the execution claim it names is no
#: longer current. Unlike the others this is not a race over one outcome: the
#: report belongs to a dead claim and can never succeed, so retrying it is not
#: caution but a permanent hold on the queue.
SETTLE_SUPERSEDED = "superseded"

_DETAIL_LIMIT = 1000
_STATUS_LIMIT = 200
_LOCK_TIMEOUT_S = 10.0
#: Last status stamped by :meth:`TerminalReportOutbox.note_absence`.
_ABSENT_STATUS = "task-absent"
#: A task id is interpolated into ``/tasks/{id}/complete``, so it must be one
#: inert path segment. httpx collapses dot segments before sending, so a stored
#: id of ``../../cluster/nodes/x`` is delivered as ``/cluster/nodes/x/complete``
#: -- with the worker's own bearer token on it. The outbox sits under ``.sdd/``,
#: which every agent on the node can write, so records are input here, not
#: trusted state. Same character rule as the approvals id in
#: :mod:`bernstein.core.orchestration.approval_gate`: leading alphanumeric (which
#: is what rules out ``.`` and ``..``), then ``[A-Za-z0-9._-]``.
_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: The only branch namespace a worker's own work lands in (see ``_push_agent_work``).
_AGENT_BRANCH_PREFIX = "agent/"
DEFAULT_BASE_DELAY_S = 1.0
DEFAULT_MAX_DELAY_S = 300.0

_OUTBOX_RELPATH = ".sdd/runtime/worker_outbox.jsonl"
_LOCK_RELPATH = ".sdd/runtime/locks/worker_outbox.lock"


@dataclass
class TerminalReport:
    """One terminal outcome a worker still owes the central server.

    Attributes:
        task_id: Task the outcome belongs to.
        outcome: One of :data:`OUTCOME_DONE`, :data:`OUTCOME_FAILED`,
            :data:`OUTCOME_UNKNOWN`.
        detail: Summary/reason text sent to the server, truncated to
            :data:`_DETAIL_LIMIT`.
        agent_branch: Remote branch holding the agent's work, captured once at
            reap time. Frozen here so every retry re-delivers the same SHA.
        agent_sha: Head SHA of :attr:`agent_branch`.
        claim_token: The execution claim this report answers, captured from the
            claim grant. Persisted for the same reason the outcome is: the
            report is replayed later, possibly after a restart, and the fence
            has to outlive the process that learned it. A token that lived only
            in memory would come back ``None``, which the server reads as an
            unfenced legacy caller and accepts -- silently reopening the hole
            the fence exists to close.
        node: Name of the worker node that produced the report; only that node
            drains it.
        report_id: Content-unique id, stable across restarts.
        attempts: Delivery attempts already made (drives the backoff schedule).
        first_seen_at: Wall-clock creation time.
        next_attempt_at: Wall-clock gate for the next attempt; persisted so a
            restart cannot collapse the backoff into an immediate retry storm.
        last_status: Last response status/error, for diagnosis.
        absences: Consecutive reads in which the central answered that this
            task does not exist. On the record, not in memory, because the only
            safe reading of that answer is "repeat it a few times before
            believing it" and the repeats must survive a restart.
    """

    task_id: str
    outcome: str
    detail: str = ""
    agent_branch: str | None = None
    agent_sha: str | None = None
    claim_token: str | None = None
    node: str = ""
    report_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    attempts: int = 0
    first_seen_at: float = field(default_factory=time.time)
    next_attempt_at: float = field(default_factory=time.time)
    last_status: str = ""
    absences: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the JSONL outbox."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TerminalReport:
        """Rebuild a record, ignoring keys from a newer worker version."""
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in raw.items() if k in known}
        kwargs.setdefault("task_id", "")
        kwargs.setdefault("outcome", OUTCOME_UNKNOWN)
        return cls(**kwargs)  # type: ignore[arg-type]


def _is_safe_task_id(task_id: object) -> bool:
    """Whether *task_id* can be interpolated into a request path unchanged.

    Shared by :meth:`TerminalReportOutbox.append` and its loader so the two
    cannot disagree about what is safe to send. The ``isinstance`` half is not
    pedantry: a hand-edited record carrying a non-string id would otherwise make
    ``fullmatch`` raise and blind the worker to every report in the file.
    """
    return isinstance(task_id, str) and _TASK_ID_RE.fullmatch(task_id) is not None


def _trusted_delivery(
    delivery: tuple[str | None, str | None] | None,
) -> tuple[str | None, str | None]:
    """Return the ``(branch, sha)`` pair only if the branch is one we may name.

    The central refuses to *fetch* a branch outside ``agent/`` but still accepts
    the completion that names it, so a forged ``main`` here would mark a task done
    while none of its work merged. Refusing the pair keeps the report truthful --
    a completion carrying no delivery metadata, which is what an in-agent curl
    completion already looks like -- instead of either losing the outcome or
    believing a ref the worker never pushed.

    A delivery whose SHA does not match its branch is left alone: the central
    compares the two itself (#6538) and the janitor verifies the posted artifact
    (#6612), so that check belongs where the remote can be consulted.
    """
    branch, sha = delivery if delivery is not None else (None, None)
    if branch is None or sha is None:
        return None, None
    if not branch.startswith(_AGENT_BRANCH_PREFIX):
        logger.error("ignoring a terminal-report delivery outside the agent namespace: %r", branch)
        return None, None
    return branch, sha


def retry_delay_s(
    attempts: int,
    base_delay_s: float,
    max_delay_s: float,
    *,
    retry_after_s: float | None = None,
) -> float:
    """Seconds to wait before the next attempt of a report.

    Args:
        attempts: Attempts already made (1-based for the first retry).
        base_delay_s: Base delay for the exponential schedule.
        max_delay_s: Cap on the delay -- and on a server-supplied Retry-After.
        retry_after_s: Server-supplied ``Retry-After`` when the response
            carried one; it wins over the computed schedule.

    Returns:
        Delay in seconds, never negative and never above *max_delay_s*.
    """
    if retry_after_s is not None:
        return min(max(retry_after_s, 0.0), max_delay_s)
    # Deferred: importing the server package pulls FastAPI into the worker
    # CLI's import graph, which does not otherwise need it.
    from bernstein.core.server.http_retry import compute_backoff

    return compute_backoff(max(attempts - 1, 0), base_delay_s, max_delay_s)


class TerminalReportOutbox:
    """Crash-safe queue of terminal reports a worker still owes the server.

    The file is the source of truth; :attr:`_records` is a reload-on-mutate
    cache. A record is appended and fsynced before the caller attempts delivery
    and is only removed once the server has acknowledged it.
    """

    def __init__(
        self,
        workdir: Path,
        *,
        node: str = "",
        base_delay_s: float = DEFAULT_BASE_DELAY_S,
        max_delay_s: float = DEFAULT_MAX_DELAY_S,
    ) -> None:
        self._path = workdir / _OUTBOX_RELPATH
        self._lock_path = workdir / _LOCK_RELPATH
        self._node = node
        self._base_delay_s = base_delay_s
        self._max_delay_s = max_delay_s
        self._records: dict[str, TerminalReport] = {}
        # Records whose durable write failed (disk full, wedged lock). Kept in
        # memory so the failure degrades to "not yet durable" rather than back
        # into the silent discard this outbox exists to prevent.
        self._volatile: dict[str, TerminalReport] = {}
        self._load()

    @property
    def path(self) -> Path:
        """Location of the durable JSONL file."""
        return self._path

    def append(
        self,
        task_id: str,
        outcome: str,
        detail: str = "",
        *,
        delivery: tuple[str, str] | None = None,
        claim_token: str | None = None,
    ) -> TerminalReport:
        """Durably queue one terminal outcome. Call this before delivering.

        Args:
            task_id: Task the outcome belongs to.
            outcome: One of the ``OUTCOME_*`` constants.
            detail: Summary/reason text for the server.
            delivery: ``(branch, sha)`` of the pushed agent work, captured once
                at reap time and replayed verbatim on every retry.
            claim_token: The execution claim this report answers, replayed
                verbatim on every retry for the same reason *delivery* is.

        Returns:
            The queued record (durable unless the write failed), or an
            unqueued one if *task_id* was refused.

        Raises:
            Nothing. The reap loop calls this once per finished agent with no
            guard around it, so a bad id is refused and logged rather than
            raised -- raising would cost the *other* finished agents their
            reports.
        """
        branch, sha = _trusted_delivery(delivery)
        report = TerminalReport(
            task_id=task_id,
            outcome=outcome,
            detail=detail[:_DETAIL_LIMIT],
            agent_branch=branch,
            agent_sha=sha,
            claim_token=claim_token,
            node=self._node,
        )
        if not _TASK_ID_RE.fullmatch(task_id):
            # Deliberately not parked in _volatile either: a retry could not
            # reach the endpoint this id names, it would only resend this node's
            # bearer token somewhere else.
            logger.error("refusing to queue a terminal report for an unsafe task id: %r", task_id)
            return report
        try:
            with cross_process_lock(self._lock_path, timeout=_LOCK_TIMEOUT_S):
                self._load()
                if report.report_id not in self._records:
                    self._records[report.report_id] = report
                    self._rewrite_locked()
        except (OSError, LockTimeout) as exc:
            logger.error("could not durably queue terminal report for task %s: %s", report.task_id, exc)
            self._volatile[report.report_id] = report
        return report

    def due(self, *, now: float | None = None) -> list[TerminalReport]:
        """Reports this node owes, gated by their backoff schedule.

        Records that are not yet due are simply absent: the caller must not
        sleep, because the worker loop also carries heartbeats and claims.
        """
        moment = self._moment(now)
        owed = (r for r in self.pending() if (not r.node or r.node == self._node) and r.next_attempt_at <= moment)
        return sorted(owed, key=lambda r: r.first_seen_at)

    def pending(self) -> list[TerminalReport]:
        """Every report still owed, durable or not, oldest first.

        A wedged peer lock defers the *disk* read to the next loop cycle rather
        than raising into the worker loop: this queue is retried forever anyway,
        so skipping one cycle is never a lost outcome. The undurable records are
        still returned in that case, because they have no copy on disk to wait
        for -- withholding them would stop all delivery for as long as the disk
        refused writes, which is the regression this method used to be.
        """
        self._flush_volatile()
        try:
            with cross_process_lock(self._lock_path, timeout=_LOCK_TIMEOUT_S):
                self._load()
                records = self._merged()
        except LockTimeout as exc:
            logger.warning("terminal-report outbox lock busy, deferring this cycle: %s", exc)
            records = dict(self._volatile)
        return sorted(records.values(), key=lambda r: r.first_seen_at)

    def _merged(self) -> dict[str, TerminalReport]:
        """Durable records plus the undurable ones, the durable copy winning.

        A report can only be in both when a flush raced its own retry; the file
        is what every peer node sees, so that copy is authoritative and the
        private one is dropped rather than delivered a second time.
        """
        return {**self._volatile, **self._records}

    @staticmethod
    def _moment(now: float | None) -> float:
        """Wall-clock instant to evaluate the backoff gate against."""
        return time.time() if now is None else now

    def note_attempt(
        self,
        report_id: str,
        status: str,
        *,
        now: float | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        """Record an unsuccessful attempt and schedule the next one.

        Retries are bounded in *delay*, not in count: capping the count would
        reintroduce the silent discard, and a server-side ack is the only thing
        that removes a record.
        """
        moment = self._moment(now)

        def _stamp(records: dict[str, TerminalReport]) -> None:
            record = records.get(report_id)
            if record is None:
                return
            record.attempts += 1
            record.absences = 0
            record.last_status = status[:_STATUS_LIMIT]
            record.next_attempt_at = moment + retry_delay_s(
                record.attempts,
                self._base_delay_s,
                self._max_delay_s,
                retry_after_s=retry_after_s,
            )

        self._apply(_stamp, report_id)

    def note_absence(self, report_id: str, *, hold_s: float, now: float | None = None) -> int:
        """Stamp one "this task does not exist" read; return the running count.

        The count is what lets a caller insist that such a verdict be repeated
        before it is acted on, so it has to be part of the record: a counter
        held in memory would restart at zero on exactly the process restart that
        a long hold invites. Any other answer (:meth:`note_attempt`) clears it,
        because a server that replies about the task proved it exists.
        """
        moment = self._moment(now)
        seen = 0

        def _bump(records: dict[str, TerminalReport]) -> None:
            nonlocal seen
            record = records.get(report_id)
            if record is None:
                return
            record.absences += 1
            seen = record.absences
            record.last_status = _ABSENT_STATUS
            record.next_attempt_at = moment + hold_s

        self._apply(_bump, report_id)
        return seen

    def settle(self, report_id: str, disposition: str) -> None:
        """Drop a record: the server has resolved it (*disposition* says how)."""
        del disposition  # recorded by the caller's log line, not on the queue

        def _drop(records: dict[str, TerminalReport]) -> None:
            records.pop(report_id, None)

        self._apply(_drop, report_id)

    def _apply(self, change: Callable[[dict[str, TerminalReport]], None], report_id: str) -> None:
        """Apply *change* under the lock and rewrite the file.

        A failed write leaves the record on disk untouched: the cost is one
        duplicate post (which the caller reconciles), versus a lost outcome if
        the removal had been assumed successful.

        A record the disk would not accept travels with the change.
        ``note_attempt`` has to stamp its backoff and ``settle`` has to retire
        it; this method used to pop the in-memory copy and never put it back, so
        the first retry of a not-yet-durable report deleted the only copy of the
        outcome it was supposed to keep retrying.
        """
        held = self._volatile.pop(report_id, None)
        survivor: TerminalReport | None = None
        if held is not None:
            holder: dict[str, TerminalReport] = {report_id: held}
            change(holder)
            survivor = holder.get(report_id)
        try:
            with cross_process_lock(self._lock_path, timeout=_LOCK_TIMEOUT_S):
                self._load()
                change(self._records)
                self._rewrite_locked()
        except (OSError, LockTimeout) as exc:
            logger.error("could not persist outbox change for report %s: %s", report_id, exc)
        if survivor is not None:
            self._volatile[report_id] = survivor

    def _rewrite_locked(self) -> None:
        """Compact the file to exactly :attr:`_records`. Caller holds the lock.

        Rewrites (not appends) go through ``os.replace`` so a reader always sees
        a whole file, and a settled report is never replayed after a restart.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(f"{json.dumps(r.to_dict(), sort_keys=True)}\n" for r in self._records.values())
        write_atomic_text(self._path, body)

    def _flush_volatile(self) -> None:
        """Retry the durable write for records whose first append failed."""
        for report_id, report in list(self._volatile.items()):
            try:
                with cross_process_lock(self._lock_path, timeout=_LOCK_TIMEOUT_S):
                    self._load()
                    if report_id not in self._records:
                        self._records[report_id] = report
                        self._rewrite_locked()
            except (OSError, LockTimeout) as exc:
                logger.warning("terminal report for task %s still undurable: %s", report.task_id, exc)
                continue
            self._volatile.pop(report_id, None)

    def _load(self) -> None:
        """Hydrate :attr:`_records` from disk, skipping unreadable lines.

        Lock-free on purpose: every writer swaps the whole file through
        ``os.replace``, so a reader sees either the old or the new file -- never
        a torn mix. The skip keeps one hand-edited or truncated line from
        blinding the worker to every report behind it.
        """
        self._records = {}
        if not self._path.is_file():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("could not read terminal-report outbox %s: %s", self._path, exc)
            return
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                record = TerminalReport.from_dict(json.loads(line))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed terminal-report record in %s: %s", self._path, exc)
                continue
            if not _is_safe_task_id(record.task_id):
                # Covers the missing id and the path-shaped one. Such a record is
                # either junk or an attempt to aim this worker's next
                # authenticated post at another endpoint; neither is deliverable,
                # so it is dropped here rather than retried forever.
                logger.error(
                    "skipping terminal-report record with an unusable task id (%r) in %s",
                    record.task_id,
                    self._path,
                )
                continue
            record.agent_branch, record.agent_sha = _trusted_delivery((record.agent_branch, record.agent_sha))
            self._records[record.report_id] = record


__all__ = [
    "OUTCOME_DONE",
    "OUTCOME_FAILED",
    "OUTCOME_UNKNOWN",
    "SETTLE_ACKED",
    "SETTLE_CONFLICT",
    "SETTLE_DUPLICATE",
    "SETTLE_GONE",
    "SETTLE_RESOLVED",
    "SETTLE_SUPERSEDED",
    "TerminalReport",
    "TerminalReportOutbox",
    "retry_delay_s",
]
