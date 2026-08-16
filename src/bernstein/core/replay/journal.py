"""EventJournal - the single canonical Merkle-chained per-run event log.

Issue #2293. Bernstein historically wrote two differently-shaped
recorders per run: the orchestrator ``RunRecorder`` (``replay.jsonl``)
and the ``ReplayGateway`` fixture log (``events.jsonl``). Recording was
gated behind ``BERNSTEIN_RECORD`` and off by default, so the documented
byte-identical replay guarantee was never exercised on a normal run and
there was no single journal to replay from.

The journal unifies the run-level recorder into one always-on,
Merkle-chained store under ``.sdd/runs/<run_id>/journal.jsonl``. Each
event is::

    event_hash = H(prev_hash, event_type, payload_hash, monotonic_index)

where ``payload_hash`` is the SHA-256 of the canonical JSON projection of
the event payload with the wall-clock envelope (``ts`` / ``elapsed_s``)
excluded, so two byte-identical executions produce the same chain of
hashes regardless of timing. The head hash content-addresses the surviving
journal state: replay divergence surfaces as a hash mismatch at a precise step
index rather than a silent drift. Only comparison with an independently sealed
head identifies that state as the complete finished journal; an unsealed clean
prefix remains unverifiable.

The journal is a drop-in for the removed ``RunRecorder``: it keeps
``record(event, **data)``, ``fingerprint()`` (aliased to the Merkle
head), ``path``, ``run_id`` and ``event_count()``. Callers need no
change.

Retention: ``BERNSTEIN_REPLAY_RETENTION`` bounds how many *past* run
journals survive on disk (oldest run directories are pruned), replacing
the old on/off ``BERNSTEIN_RECORD`` gate with a size-control knob. The
active run's chain is never rotated mid-run, so ``verify`` always walks
an intact chain.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.security.path_containment import PathContainmentError, contained_path

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)


class JournalPathError(PathContainmentError):
    """Raised for a run id that cannot safely name a journal directory.

    Subclasses :class:`ValueError` (through
    :class:`~bernstein.core.security.path_containment.PathContainmentError`)
    so callers that already handle a bad run id with ``except ValueError``
    keep working unchanged.
    """


#: Name of the canonical per-run event journal inside ``.sdd/runs/<id>/``.
JOURNAL_FILENAME = "journal.jsonl"

#: Env var bounding how many past run journals survive on disk. Replaces
#: the old ``BERNSTEIN_RECORD`` on/off gate with a size-control knob.
#: Unset or non-positive means keep everything.
RETENTION_ENV_VAR = "BERNSTEIN_REPLAY_RETENTION"

#: Envelope fields that vary across runs even when execution is identical.
#: Excluded from ``payload_hash`` so two byte-identical runs chain to the
#: same head regardless of timing (mirrors the legacy recorder policy,
#: issue #1851).
_NON_DETERMINISTIC_FIELDS = frozenset({"ts", "elapsed_s", "index", "prev_hash", "payload_hash", "event_hash"})

_GENESIS_HASH = ""

#: A run_id names exactly one journal directory and must be a single safe path
#: segment. This mirrors ``run_service.paths.validate_run_id``: an anchored
#: allowlist match then a return of the checked value.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _validated_run_id(run_id: str) -> str:
    """Return *run_id* unchanged when it is a safe path segment, else raise.

    Raises:
        JournalPathError: The id is ``.``, ``..``, or falls outside the
            allowlisted alphabet.
    """
    if run_id in {".", ".."} or not _RUN_ID_RE.match(run_id):
        raise JournalPathError(f"unsafe run_id for journal path: {run_id!r}")
    return run_id


def run_journal_path(sdd_dir: Path, run_id: str) -> Path:
    """Return the run's journal path, contained under ``<sdd>/runs``.

    Every reader of a run journal must derive its path here rather than
    rebuilding ``<sdd>/runs/<run_id>/journal.jsonl`` by hand, so that a
    crafted run id cannot address a journal outside the runs root and a
    symlinked run directory cannot redirect the read. This is the run-level
    twin of ``checkpoint_retry.task_journal_path``.

    ``verify_journal`` is not a substitute: it is an unkeyed Merkle
    recompute, so a journal planted outside the tree verifies cleanly.

    Args:
        sdd_dir: The project ``.sdd`` directory.
        run_id: The run whose journal to locate.

    Returns:
        The containment-checked journal path.

    Raises:
        JournalPathError: The run id is not a safe path segment, or the
            resolved journal escapes the runs root.
    """
    safe_run_id = _validated_run_id(run_id)
    try:
        return contained_path(sdd_dir / "runs", safe_run_id, JOURNAL_FILENAME, label="run id")
    except PathContainmentError as exc:
        raise JournalPathError(f"run_id escapes the journal runs root: {run_id!r}") from exc


def contained_run_journal(runs_root: Path, entry_name: str, filename: str = JOURNAL_FILENAME) -> Path | None:
    """Return the journal path for an iterated run directory, or ``None``.

    For the sweep case: the caller obtained *entry_name* by iterating
    *runs_root*, so it cannot carry ``..`` or a separator. That covers only
    half the threat. A directory entry with a perfectly ordinary name can be
    a **symlink** pointing outside the runs root - the name is innocent, the
    target is not - and iteration says nothing about what the entry resolves
    to. Containment is what closes that half, so sweeps re-derive through
    the same barrier as targeted lookups.

    Returns ``None`` for an entry that escapes, so a sweep skips it and
    keeps going rather than aborting the whole pass over one bad entry.

    Args:
        runs_root: The directory being iterated.
        entry_name: A directory entry name from that iteration.
        filename: Journal filename to append.

    Returns:
        The contained journal path, or ``None`` when the entry escapes.
    """
    try:
        return contained_path(runs_root, entry_name, filename, label="run directory")
    except PathContainmentError:
        logger.warning(
            "skipping run directory %r: it resolves outside %s",
            entry_name,
            runs_root,
        )
        return None


def _payload_hash(event_type: str, payload: dict[str, Any]) -> str:
    """Return the SHA-256 of the canonical, timing-excluded payload.

    The ``event`` type and the decision-relevant payload keys are hashed;
    the wall-clock envelope and the derived chain fields are dropped so a
    faithful replay - which differs only in timing - hashes identically.
    """
    projected = {k: v for k, v in payload.items() if k not in _NON_DETERMINISTIC_FIELDS}
    projected["event"] = event_type
    canonical = json.dumps(projected, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_event_hash(*, prev_hash: str, event_type: str, payload_hash: str, index: int) -> str:
    """Return ``event_hash = H(prev_hash, event_type, payload_hash, index)``.

    The pre-image is canonical JSON of the ordered field tuple, so the
    digest is stable across processes and platforms.
    """
    preimage = json.dumps(
        {
            "prev_hash": prev_hash,
            "event_type": event_type,
            "payload_hash": payload_hash,
            "index": index,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()


@dataclass(frozen=True, slots=True)
class JournalLoadResult:
    """Rows accepted by the tolerant reader and the input it discarded.

    ``events`` is the ordinary replay projection. ``discarded_line_indices``
    names every 0-based physical non-blank line that the tolerant reader could
    not represent as a journal object. Keeping both in one result makes
    tolerance observable without making ordinary readers strict.
    """

    events: list[dict[str, Any]]
    discarded_line_indices: tuple[int, ...] = ()

    @property
    def discarded_count(self) -> int:
        """Number of physical non-blank lines omitted from ``events``."""
        return len(self.discarded_line_indices)


class JournalCoverageStatus(StrEnum):
    """Whether every non-blank physical line entered chain verification."""

    COMPLETE = "complete"
    PARTIAL = "partial"


class JournalIdentityStatus(StrEnum):
    """Relationship between a journal and an independent sealed commitment."""

    VERIFIED = "verified"
    MISMATCHED = "mismatched"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class JournalSeal:
    """Independent commitment to one finished journal state."""

    head: str
    event_count: int


@dataclass(frozen=True, slots=True)
class JournalRepairResult:
    """Outcome of :func:`repair_journal_tail`.

    Attributes:
        repaired: ``True`` when a torn trailing fragment was truncated.
        removed_line_indices: 0-based physical lines truncated away
            (empty when the journal was clean).
        event_count: Number of surviving events after repair (or before
            it for a clean journal).
        head: Surviving chain head after repair (or before it for a
            clean journal).
    """

    repaired: bool
    removed_line_indices: tuple[int, ...] = ()
    event_count: int = 0
    head: str = ""


@dataclass(frozen=True, slots=True)
class JournalVerifyResult:
    """Separate chain, reader-coverage, and sealed-identity verdicts.

    Attributes:
        chain_consistent: Whether the parsed rows recompute from genesis.
            This proves only that the surviving rows form a valid prefix; it
            does not identify the complete journal.
        coverage: Whether every non-blank physical line reached the chain
            verifier.
        identity: Whether the parsed journal matches an independent seal, is
            known not to match it, or has no seal and is therefore
            unverifiable.
        count: Number of parsed rows supplied to chain verification. This is
            intentionally not a claim about physical-file coverage.
        divergent_index: 0-based index of the first row whose stored hash
            does not match the recomputed hash (or a broken ``prev_hash``
            link), or ``None`` when the chain is intact.
        expected_hash: Recomputed hash at :attr:`divergent_index`.
        actual_hash: Stored hash at :attr:`divergent_index`.
        head: Final head when the parsed chain is consistent; otherwise the
            head of the verified prefix before the first divergence. Empty for
            no parsed rows.
        discarded_line_indices: 0-based physical lines hidden by tolerant
            parsing.
        errors: Human-readable divergence explanations.
    """

    chain_consistent: bool
    coverage: JournalCoverageStatus
    identity: JournalIdentityStatus
    count: int
    divergent_index: int | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None
    head: str = ""
    discarded_line_indices: tuple[int, ...] = ()
    errors: list[str] = field(default_factory=list[str])


#: Cheap identity-and-length token for a journal file: ``(st_ino, st_dev,
#: st_size)``. Equality means "same file, same length".
#:
#: ``st_mtime_ns`` is deliberately *not* in here, and it was measured before
#: it was left out. It would catch a same-length in-place rewrite only when
#: the clock happened to tick between the two stats: on an ext4 tree the
#: journal timestamp advances in ~1 ms steps, so 1859 of 2000 same-length
#: rewrites produced a byte-identical ``st_mtime_ns``. A guard that fires 7%
#: of the time is worse here than one that never fires, because it makes the
#: failure irreproducible - a repairer author would test their repair, watch
#: the count update by luck, and ship without the
#: :meth:`EventJournal.invalidate_count` call that the other 93% needs. With
#: the field left out the rule is flat and testable in both directions:
#: anything that changes length or identity is caught, a same-length rewrite
#: is never caught and must invalidate explicitly.
_StatToken = tuple[int, int, int]


class EventJournal:
    """Append-only Merkle-chained per-run event journal.

    Drop-in replacement for the removed ``RunRecorder``: same
    ``record`` / ``fingerprint`` / ``path`` / ``run_id`` surface, plus a
    Merkle head and per-step verification.

    Thread-safe for the single-writer orchestrator tick loop; the append
    critical section (sequence + head + file write) runs under one lock.

    Args:
        run_id: Unique identifier for the run.
        sdd_dir: Path to the ``.sdd`` directory.
    """

    def __init__(self, run_id: str, sdd_dir: Path) -> None:
        self._run_id = run_id
        self._runs_root = sdd_dir / "runs"
        # Path-injection barrier (py/path-injection). A run_id names one journal
        # directory and must be a single safe path segment. The writer shares
        # ``run_journal_path`` with every run-journal reader, so there is one
        # definition of where a run journal lives and one barrier guarding it:
        # the path is the normalised, containment-checked value, never the raw
        # join, so every filesystem sink below is built from a location proven
        # to sit under the runs root even when the run directory is a symlink.
        self._path = run_journal_path(sdd_dir, run_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Reentrant on purpose. ``record`` dispatches the post-append observer
        # while still holding this lock (see :meth:`record`), and
        # :meth:`event_count` must take it to read the count cache coherently.
        # With a plain ``Lock`` an observer that asks the journal how many
        # events it now holds - the obvious thing for a projection to do -
        # deadlocks against its own append. Measured: the non-reentrant version
        # of this change hangs that call permanently.
        self._lock = threading.RLock()
        self._index = 0
        # Cached ``(stat token, usable event count, tail ends with newline)``,
        # or ``None`` for "not known". An absent file is never cached:
        # :meth:`_stat_token` already recognises it in O(1). The third field
        # is what makes the carry-forward in :meth:`record` safe over a
        # crash-torn tail; see there. See :meth:`_stat_token`.
        self._count_cache: tuple[_StatToken, int, bool] | None = None
        self._head = _GENESIS_HASH
        self._start_ts: float = time.time()
        self._observer: Callable[[dict[str, Any]], None] | None = None
        self._prune_old_runs()

    @classmethod
    def resume(cls, run_id: str, sdd_dir: Path) -> EventJournal:
        """Open a journal whose file may already exist, recovering the tail.

        The plain constructor targets a fresh run: it starts the chain at
        index 0 / genesis even when the file already has rows, which is
        correct for the orchestrator's one-journal-per-run lifetime but
        would break the Merkle chain for journals appended across process
        boundaries (for example a task's checkpoint journal, which a later
        retry decision extends from a different process).

        ``resume`` re-verifies the existing chain and continues from its
        tail. It fails closed: a journal whose chain does not recompute is
        refused rather than silently extended from a poisoned anchor.

        Args:
            run_id: Unique identifier for the run.
            sdd_dir: Path to the ``.sdd`` directory.

        Returns:
            A journal positioned at the verified chain tail (or at genesis
            for a missing/empty file).

        Raises:
            ValueError: The existing chain fails verification.
        """
        journal = cls(run_id, sdd_dir)
        loaded = load_events(journal.path)
        events = loaded.events
        if loaded.discarded_line_indices:
            joined = ", ".join(str(index) for index in loaded.discarded_line_indices)
            tail = _torn_tail_indices(journal.path)
            if tail:
                raise ValueError(
                    f"cannot resume journal {journal.path}: reader discarded physical line(s): {joined}; "
                    f"the tail is a torn write — repair it first with 'bernstein replay repair {run_id}'"
                )
            raise ValueError(f"cannot resume journal {journal.path}: reader discarded physical line(s): {joined}")
        if not events:
            return journal
        result = verify_journal(journal.path)
        if not result.chain_consistent or result.coverage != JournalCoverageStatus.COMPLETE:
            msg = f"cannot resume journal {journal.path}: {'; '.join(result.errors) or 'chain verification failed'}"
            raise ValueError(msg)
        journal._index = len(events)
        journal._head = str(events[-1].get("event_hash", ""))
        return journal

    @property
    def run_id(self) -> str:
        """The run identifier this journal is writing to."""
        return self._run_id

    @property
    def path(self) -> Path:
        """Path to the canonical journal JSONL file."""
        return self._path

    def head(self) -> str:
        """Return the Merkle identifier of the journal state seen so far.

        The head identifies the surviving prefix; only an independent seal
        can establish that the prefix is the complete finished journal.
        """
        return self._head

    def fingerprint(self) -> str:
        """Return the journal-state fingerprint - the Merkle head.

        Aliased to :meth:`head` so callers that used
        ``RunRecorder.fingerprint`` keep a stable API while the value is
        now the chain head rather than a whole-file rehash.
        """
        return self._head

    def set_observer(self, observer: Callable[[dict[str, Any]], None] | None) -> None:
        """Register a post-append observer (or clear it with ``None``).

        The observer receives each successfully appended entry dict (chain
        fields included) after the append commits. It is a read-only tap
        for projections such as live OTel export (#2526): observer
        exceptions are swallowed with a warning and can never fail or
        reorder an append. Single observer; the journal stays a
        single-writer structure.
        """
        self._observer = observer

    def record(self, event: str, **data: Any) -> None:
        """Append one Merkle-chained event to the journal.

        Args:
            event: Event type (e.g. ``"task_claimed"``).
            **data: Arbitrary decision payload. Wall-clock fields are
                excluded from the hash but kept on the row for operators.
        """
        with self._lock:
            # Read the count the file holds *before* this append, but only
            # when it is already known - never by scanning. A fresh journal
            # whose file does not exist yet holds 0 events, which is knowable
            # without touching the disk, so the common "construct, then append"
            # path primes the cache on its first record and never scans at all.
            #
            # The carry is only sound when the file ends on a line boundary.
            # ``open("a")`` resumes at the last byte, so appending to a
            # crash-torn fragment glues the new row onto it and produces one
            # unusable line where the arithmetic assumed two usable ones -
            # ``prior + 1`` would then overcount for the rest of the journal's
            # life. That state is reachable: the plain constructor accepts a
            # torn file (only ``resume`` refuses one), so a scan can legally
            # prime the cache from it. The damage is not bounded by one event
            # either - a crash that lands between a complete row and its
            # newline leaves a file the scan reports as *undamaged*, and
            # gluing onto it destroys the row that was already there as well
            # as miscounting the new one. See
            # ``test_appending_to_a_newline_less_tail_does_not_destroy_a_row``.
            prior: int | None = None
            pre_token: _StatToken | None = None
            try:
                pre_token = self._stat_token()
            except OSError:
                pass  # unreadable: the count before this append is unknowable
            else:
                cached = self._count_cache
                if cached is not None and cached[0] == pre_token and cached[2]:
                    prior = cached[1]
                elif pre_token is None:
                    prior = 0
            index = self._index
            prev_hash = self._head
            p_hash = _payload_hash(event, data)
            e_hash = compute_event_hash(
                prev_hash=prev_hash,
                event_type=event,
                payload_hash=p_hash,
                index=index,
            )
            entry: dict[str, Any] = {
                "ts": time.time(),
                "elapsed_s": round(time.time() - self._start_ts, 3),
                "index": index,
                "event": event,
                "prev_hash": prev_hash,
                "payload_hash": p_hash,
                "event_hash": e_hash,
            }
            entry.update(data)
            line = json.dumps(entry, default=str)
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as exc:
                logger.warning("EventJournal: failed to write event %r: %s", event, exc)
                # The cache is deliberately left alone. A failed append either
                # wrote nothing - in which case it is still correct - or left
                # bytes behind, and an append cannot shrink a file, so those
                # bytes move ``st_size`` and the next count rescans. Dropping
                # it here would be a second mechanism for a case the token
                # already covers, and mutation-testing confirms no behaviour
                # distinguishes the two.
                return
            self._count_cache = self._cache_after_append(prior, pre_token, len((line + "\n").encode("utf-8")))
            self._index = index + 1
            self._head = e_hash
            # Dispatch while the append lock still establishes total order.
            # Calling after releasing it lets a later writer deliver index N+1
            # before index N, which corrupts order-sensitive projections such
            # as the incremental OTel span tree. The live observer only queues
            # onto BatchSpanProcessor, so it performs no network I/O here.
            observer = self._observer
            if observer is not None:
                try:
                    observer(entry)
                except Exception as exc:
                    logger.warning("EventJournal observer failed for event %r: %s", event, exc)

    def event_count(self) -> int:
        """Return the number of events recorded so far.

        The relationship to the other reader, in one sentence:
        ``event_count() == len(load_events(path).events)``. Counting
        *usable* events rather than physical lines is not a preference -
        it is the only count the rest of the class already agrees with.
        :meth:`resume` continues the chain from ``len(events)``, so a row
        appended after a malformed one carries that index and not a line
        number; the read side recovers the same value from the row's own
        ``index`` field (``run_artifacts._row_to_record``). Every caller
        spells this ``event_count() - 1`` to name the row it just wrote,
        and a physical-line count would name a row that is not there.

        Sharing the scan rather than re-implementing it is also what keeps
        the two readers on one decode policy: a strict decode here made a
        journal torn mid-character raise :class:`UnicodeDecodeError` out
        of a method whose failure mode is documented as ``0``, because
        that error derives from :class:`ValueError` and not from
        :class:`OSError`. The handler below is now exactly what it says -
        an unreadable file - since no decode error can reach it.

        The scan itself costs a JSON parse per row where the old one cost
        a strip, so this method caches its result and returns it in O(1)
        for as long as the file is unchanged. The cache never weakens the
        invariant above: it is keyed on a ``stat`` token and is dropped
        the moment the file stops being the one that was counted.

        **The one change this cannot see is an in-place rewrite that keeps
        the byte length**, and it is not seen *deterministically* rather
        than most of the time - see :data:`_StatToken` for why that is the
        deliberate choice and for the measurement behind it. A repairer
        that rewrites a row without changing its length must call
        :meth:`invalidate_count`. Nothing in this tree rewrites a journal
        in place today; the method exists so that whatever does can.
        """
        with self._lock:
            try:
                return self._count_locked()
            except OSError:
                self._count_cache = None
                return 0

    def invalidate_count(self) -> None:
        """Drop the cached event count, forcing the next call to rescan.

        Required after any in-place rewrite of the journal file that does
        not change its length - a tail repair that substitutes bytes
        rather than truncating, for instance. Every other mutation
        (append, truncate, replace) moves ``st_size`` or the inode and is
        caught without help; the same-length case is never caught, by
        design, and :data:`_StatToken` records why.
        """
        with self._lock:
            self._count_cache = None

    def _stat_token(self) -> _StatToken | None:
        """Return a cheap change token for the journal file.

        Returns:
            ``(st_ino, st_dev, st_size)``, or ``None`` when the file is
            provably absent - which is a *known* count of zero, not an
            unknown one.

        Raises:
            OSError: The file exists but could not be stat-ed. Left to the
                caller so an unreadable journal keeps reporting ``0``
                through the same handler as before rather than being
                mistaken for an empty one.
        """
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_dev, st.st_size)

    def _tail_is_clean(self) -> bool:
        """Whether the journal file ends on a line boundary.

        An empty or absent file is clean - there is no partial row to glue
        onto. Costs one seek and one byte, and is only paid on the scan
        path, never per append.

        This asks the *bytes*, not the scan, and the difference is load
        bearing. A crash that lands between a complete row and its newline
        leaves every line parsable, so the tolerant reader discards nothing
        and reports a perfectly healthy file - the one torn state that
        ``discarded_line_indices`` cannot see. Deriving the boundary from
        the scan's own damage report would therefore reintroduce the worst
        of the three divergences it exists to close.
        """
        try:
            with self._path.open("rb") as handle:
                if handle.seek(0, os.SEEK_END) == 0:
                    return True
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) == b"\n"
        except OSError:
            return False

    def _cache_after_append(
        self,
        prior: int | None,
        pre_token: _StatToken | None,
        written: int,
    ) -> tuple[_StatToken, int, bool] | None:
        """Pair ``prior + 1`` with the file's token, if this append explains it.

        Returns ``None`` - "not known" - when the count before the append was
        unknown, or when the file after the append is not exactly the file
        before it plus *written* bytes. That last check is what keeps a
        foreign append landing between the write and this stat from being
        sealed under a token it does not describe: the mismatched entry would
        otherwise be returned in O(1) forever, because it names the current
        file and so never expires. It is the same conservative refusal
        :meth:`_count_locked` makes after its scan, on the write side.
        """
        if prior is None:
            return None
        try:
            token = self._stat_token()
        except OSError:
            return None
        if token is None:
            return None
        expected_size = (pre_token[2] if pre_token is not None else 0) + written
        if token[2] != expected_size:
            return None
        if pre_token is not None and token[:2] != pre_token[:2]:
            return None
        # This writer just terminated its own row, so the tail is a boundary.
        return (token, prior + 1, True)

    def _count_locked(self) -> int:
        """Count usable events, from cache when the file has not moved.

        Caller holds the lock.
        """
        token = self._stat_token()
        cached = self._count_cache
        if cached is not None and cached[0] == token:
            return cached[1]
        if token is None:
            # Nothing to cache: a missing file is recognised by the token
            # alone on every later call, without a scan, so an entry here
            # could never be read. Mutation-tested - writing one changes
            # no observable behaviour.
            return 0
        count = len(load_events(self._path).events)
        # Re-stat after the scan. A writer that appended *while* the scan
        # was running would otherwise get a count from one version of the
        # file sealed in under the token of another, and every later call
        # would return that stale number in O(1). Unchanged across the
        # scan means the count describes the file the token names.
        clean = self._tail_is_clean()
        self._count_cache = (token, count, clean) if self._stat_token() == token else None
        return count

    def verify(self) -> JournalVerifyResult:
        """Recompute the parsed chain and report the first divergent step.

        Walks every row, recomputing ``payload_hash`` and ``event_hash``
        from the on-disk payload and checking the ``prev_hash`` link. The
        first row whose recomputed hash differs from its stored hash (or
        whose ``prev_hash`` breaks the chain) is reported by index, so an
        injected non-deterministic result surfaces as a hash mismatch at a
        precise step rather than a silent drift. Reader coverage is reported
        separately, and complete journal identity remains ``unverifiable``
        because this convenience method has no independent seal.
        """
        return verify_journal(self._path)

    def _prune_old_runs(self) -> None:
        """Prune oldest run directories beyond ``BERNSTEIN_REPLAY_RETENTION``.

        Retention is applied over past *runs*, never mid-run, so the
        active chain stays intact for ``verify``. Non-positive or unset
        retention keeps everything.
        """
        retention = _retention_limit()
        if retention <= 0 or not self._runs_root.exists():
            return
        run_dirs = sorted(
            (d for d in self._runs_root.iterdir() if d.is_dir()),
            key=lambda d: d.name,
        )
        excess = len(run_dirs) - retention
        for stale in run_dirs[:excess]:
            if stale.name == self._run_id:
                continue
            with contextlib.suppress(OSError):
                _remove_tree(stale)


def _retention_limit() -> int:
    raw = os.environ.get(RETENTION_ENV_VAR, "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _remove_tree(path: Path) -> None:
    """Recursively remove a run directory (no ``shutil`` dependency)."""
    for child in path.iterdir():
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    path.rmdir()


class JournalParseError(ValueError):
    """Raised by ``load_events(strict=True)`` for an untrustworthy row.

    Carries the 0-based physical line index so a diagnostic caller can name
    the exact on-disk location that refused to parse.
    """


def _is_decodable(line: str) -> bool:
    """Whether *line* came back from a lossless UTF-8 decode.

    ``load_events`` decodes with ``errors="surrogateescape"``, which maps each
    undecodable byte to a lone surrogate in U+DC80..U+DCFF rather than raising.
    Those code points cannot be encoded back to UTF-8, so a failed re-encode is
    an exact test for "this physical line held bytes that are not UTF-8" - and
    it is exact in the other direction too: no lossless decode can produce a
    lone surrogate, because UTF-8 cannot carry one.

    The ASCII fast path matters rather than being decoration. Every row this
    package writes is ASCII (``json.dumps`` escapes non-ASCII by default), so
    ``str.isascii`` - a flag lookup on CPython, not a scan - answers for the
    whole journal in the common case and no line is encoded twice.
    """
    if line.isascii():
        return True
    try:
        line.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def load_events(path: Path, *, strict: bool = False) -> JournalLoadResult:
    """Load all events from a journal JSONL file in append order.

    One scan implementation serves both reader policies, so a journal-format
    change can never make a diagnostic reader drift from the rest of replay:

    * tolerant (default): malformed lines are skipped so a partial trailing
      write cannot wedge an ordinary reader;
    * ``strict=True``: any non-blank line that does not decode as a JSON
      object raises :class:`JournalParseError` naming the 0-based physical
      line index. Diagnostic readers (``bernstein audit diagnose``) use this
      so no reported index can ever count parsed rows rather than physical
      journal lines, and no finding is derived from a filtered sequence.

    "Malformed" includes *undecodable*. A crash partway through an append can
    land inside a multi-byte character as easily as between two, and a strict
    decode would make the same class of crash produce two different outcomes
    depending on where in the byte stream it stopped: a discarded line index
    in one case, a bare ``UnicodeDecodeError`` out of the reader in the other.
    Bytes that are not valid UTF-8 are therefore surfaced through the same two
    policies as unparsable JSON - discarded by the tolerant reader, raised as
    :class:`JournalParseError` naming the physical line by the strict one.
    """
    events: list[dict[str, Any]] = []
    discarded: list[int] = []
    if not path.exists():
        return JournalLoadResult(events=events)
    # Binary handle, decoded through the *same* TextIOWrapper machinery as
    # ``path.open(encoding="utf-8")``: universal-newline splitting is what
    # defines a physical line here, and re-implementing it over bytes would
    # silently renumber every index this function reports (a lone ``\r`` ends
    # a line in text mode and does not in ``bytes.split(b"\n")``). Only the
    # error policy changes. ``surrogateescape`` rather than ``replace``
    # because it is reversible: a caller inspecting or repairing a torn tail
    # still has the original bytes, which ``replace`` would destroy.
    with path.open("rb") as raw_file, io.TextIOWrapper(raw_file, encoding="utf-8", errors="surrogateescape") as f:
        for lineno, raw in enumerate(f):
            line = raw.strip()
            if not line:
                continue
            if not _is_decodable(line):
                if strict:
                    raise JournalParseError(f"undecodable bytes at physical line {lineno} (not valid UTF-8)")
                discarded.append(lineno)
                continue
            try:
                decoded: object = json.loads(line)
            except json.JSONDecodeError as exc:
                if strict:
                    raise JournalParseError(f"unparsable line at physical line {lineno} ({exc.msg})") from exc
                discarded.append(lineno)
                continue
            if not isinstance(decoded, dict):
                if strict:
                    raise JournalParseError(f"non-object row at physical line {lineno}")
                discarded.append(lineno)
                continue
            row = cast("dict[str, Any]", decoded)
            if strict:
                _validate_strict_row(row, lineno)
            events.append(row)
    return JournalLoadResult(events=events, discarded_line_indices=tuple(discarded))


def _torn_tail_indices(path: Path) -> tuple[int, ...]:
    """Return the trailing-fragment physical lines, or ``()`` if none.

    A torn write truncates the *last* line and nothing else: the final
    physical line is unparsable and every discarded line is at the end
    of the file. A discard anywhere before the last non-blank line is
    corruption (a hole in the middle of the journal), which must never
    share the repair code path.

    Returns a tuple of 0-based physical line indices that can be
    truncated away, or ``()`` when the journal has nothing to repair.
    """
    loaded = load_events(path)
    discarded = loaded.discarded_line_indices
    if not discarded:
        return ()
    # The discarded lines must be a *trailing* run: the last discarded
    # index must be the last physical line of the file, and no discarded
    # index may be followed by a line that the tolerant reader accepted.
    # We re-read the physical lines to answer "is this the tail" without
    # re-parsing (the issue's "answerable without a second scan" promise
    # is about parsing; counting lines is cheap and exact).
    # ``errors="surrogateescape"`` matches the decode policy the tolerant
    # reader uses. A tear in the middle of a multi-byte character is one
    # of the two shapes this function exists to identify, and a strict
    # decode raises UnicodeDecodeError on it -- which derives from
    # ValueError, so ``except OSError`` would not catch it and the
    # function would propagate instead of reporting the tail. Widening
    # the except clause is the wrong fix: it returns "nothing to repair"
    # for exactly the journal that needs repairing.
    try:
        with path.open(encoding="utf-8", errors="surrogateescape") as f:
            physical_lines = f.readlines()
    except OSError:
        return ()
    last_physical = len(physical_lines) - 1
    if max(discarded) != last_physical:
        return ()
    discarded_set = set(discarded)
    # Every line after the first discarded index must be discarded too;
    # otherwise a readable line sits between fragments (corruption).
    for lineno in range(min(discarded), last_physical + 1):
        if lineno not in discarded_set:
            return ()
    return tuple(discarded)


def repair_journal_tail(
    path: Path,
    *,
    seal: JournalSeal | None = None,
) -> JournalRepairResult:
    """Truncate a crash-torn trailing fragment so the journal can resume.

    A crash partway through appending leaves a truncated final line with
    no trailing newline. ``EventJournal.resume`` refuses such a journal
    (its tolerant read discarded the physical line), and with no repair
    path the task would be unresumable for good. This repairs exactly
    that one failure mode: it truncates the trailing fragment and
    nothing else, restoring byte-for-byte the prefix the surviving chain
    head already commits to.

    The repair is conservative:

    * a discard anywhere but the end of the file is corruption, not a
      torn write, and is refused with a different message;
    * if an external seal exists and the truncated journal would not
      match it, the repair is refused *before* any write, so the
      evidence survives;
    * a journal with nothing to truncate reports a no-op.

    Args:
        path: Path to a ``journal.jsonl`` file.
        seal: Optional independent finished-journal commitment. When
            given, the truncated result must match it or the repair is
            refused before writing.

    Returns:
        A :class:`JournalRepairResult` describing what was done.

    Raises:
        ValueError: The discarded lines are not a trailing fragment
            (corruption), or the truncated result would not match
            *seal*.
    """
    loaded = load_events(path)
    discarded = loaded.discarded_line_indices
    if not discarded:
        events = loaded.events
        head = str(events[-1].get("event_hash", "")) if events else ""
        return JournalRepairResult(
            repaired=False,
            event_count=len(events),
            head=head,
        )

    torn = _torn_tail_indices(path)
    if not torn:
        joined = ", ".join(str(index) for index in discarded)
        raise ValueError(
            f"refusing repair of {path}: reader discarded physical line(s) {joined} "
            "in the middle of the journal (corruption, not a torn write); "
            "repair truncates only a trailing fragment"
        )

    events = loaded.events
    surviving_head = str(events[-1].get("event_hash", "")) if events else ""
    surviving_count = len(events)
    if seal is not None and (surviving_head != seal.head or surviving_count != seal.event_count):
        raise ValueError(
            f"refusing repair of {path}: truncated journal (head={surviving_head or '(empty)'}, "
            f"events={surviving_count}) does not match the external seal "
            f"(head={seal.head or '(empty)'}, events={seal.event_count}); "
            "the journal is sealed and this command is not its authority"
        )

    # Truncate to just before the first discarded physical line. Every
    # byte up to that line belongs to the surviving chain, so the prefix
    # is restored exactly (issue: "removing it restores exactly the
    # bytes the surviving head already commits to").
    #
    # os.truncate() is a single metadata operation that never rewrites
    # the surviving bytes. Reading the prefix and writing it back would
    # open the file with "w", zeroing it before the rewrite: a crash in
    # that window destroys a journal whose only damage was a torn tail,
    # which is the failure this command exists to repair. Counting the
    # cut in bytes also avoids a decode/encode round-trip of bytes that
    # are supposed to come through untouched.
    raw = path.read_bytes()
    cut = 0
    for _ in range(min(torn)):
        newline = raw.find(b"\n", cut)
        if newline == -1:
            cut = len(raw)
            break
        cut = newline + 1
    os.truncate(path, cut)

    return JournalRepairResult(
        repaired=True,
        removed_line_indices=torn,
        event_count=surviving_count,
        head=surviving_head,
    )


def _validate_strict_row(row: dict[str, Any], lineno: int) -> None:
    """Shape-check one journal row for the strict (diagnostic) reader.

    ``verify_journal`` recomputes hashes with tolerant ``.get`` defaults, so
    a row missing or mistyping its chain fields would surface downstream as
    a *chain break* - a cryptographic verdict - when the honest verdict is
    "malformed input". The strict reader therefore refuses such a row up
    front, naming the physical line, before any head or chain computation
    can be derived from it.

    Raises:
        JournalParseError: A required field is missing or carries the wrong
            primitive type.
    """
    event = row.get("event")
    if not isinstance(event, str) or not event:
        raise JournalParseError(f"row at physical line {lineno} has a missing or non-string 'event'")
    index = row.get("index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise JournalParseError(f"row at physical line {lineno} has a missing or non-integer 'index'")
    if not isinstance(row.get("prev_hash"), str):
        raise JournalParseError(f"row at physical line {lineno} has a missing or non-string 'prev_hash'")
    for hash_field in ("payload_hash", "event_hash"):
        value = row.get(hash_field)
        if not isinstance(value, str) or not value:
            raise JournalParseError(f"row at physical line {lineno} has a missing or empty {hash_field!r}")


def verify_journal(path: Path, *, seal: JournalSeal | None = None) -> JournalVerifyResult:
    """Verify parsed-chain consistency, reader coverage, and journal identity.

    Args:
        path: Path to a ``journal.jsonl`` file.
        seal: Independent finished-journal commitment. Without one, identity
            is ``unverifiable`` even when the parsed chain is consistent.

    Returns:
        A :class:`JournalVerifyResult` whose three verdict dimensions must be
        interpreted independently.
    """
    journal_exists = path.exists()
    loaded = load_events(path)
    chain = verify_events(loaded.events)
    coverage = JournalCoverageStatus.COMPLETE if not loaded.discarded_line_indices else JournalCoverageStatus.PARTIAL
    errors = list(chain.errors)
    if loaded.discarded_line_indices:
        joined = ", ".join(str(index) for index in loaded.discarded_line_indices)
        errors.append(f"reader discarded unparsable or non-object physical line(s): {joined}")

    identity = JournalIdentityStatus.UNVERIFIABLE
    if seal is not None:
        if (
            journal_exists
            and chain.chain_consistent
            and coverage == JournalCoverageStatus.COMPLETE
            and chain.head == seal.head
            and chain.count == seal.event_count
        ):
            identity = JournalIdentityStatus.VERIFIED
        else:
            identity = JournalIdentityStatus.MISMATCHED
            if not journal_exists:
                errors.append("journal file is missing but an external seal exists")
            if chain.count != seal.event_count:
                errors.append(f"journal holds {chain.count} events but the seal commits to {seal.event_count}")
            if chain.head != seal.head:
                errors.append(
                    f"journal head {chain.head or '(empty)'} does not match sealed head {seal.head or '(empty)'}"
                )

    return JournalVerifyResult(
        chain_consistent=chain.chain_consistent,
        coverage=coverage,
        identity=identity,
        count=chain.count,
        divergent_index=chain.divergent_index,
        expected_hash=chain.expected_hash,
        actual_hash=chain.actual_hash,
        head=chain.head,
        discarded_line_indices=loaded.discarded_line_indices,
        errors=errors,
    )


def verify_events(events: list[dict[str, Any]]) -> JournalVerifyResult:
    """Recompute the chain over in-memory journal rows.

    The walk :func:`verify_journal` performs, factored out so a verifier
    holding an embedded copy of the rows (e.g. a run receipt, issue #2924)
    runs the *exact same* recompute as one holding the live file. The rows
    only need the chain fields (``event``, ``prev_hash``, ``event_hash``)
    plus the decision payload; wall-clock fields are excluded from the
    hash either way.

    Args:
        events: Journal rows in append order.

    Returns:
        A result reporting a consistent empty chain with complete in-memory
        coverage and unverifiable identity (there is no external seal here).
    """
    if not events:
        return JournalVerifyResult(
            chain_consistent=True,
            coverage=JournalCoverageStatus.COMPLETE,
            identity=JournalIdentityStatus.UNVERIFIABLE,
            count=0,
        )

    prev_hash = _GENESIS_HASH
    for i, row in enumerate(events):
        event_type = str(row.get("event", ""))
        payload = {k: v for k, v in row.items() if k not in _NON_DETERMINISTIC_FIELDS}
        expected_payload_hash = _payload_hash(event_type, payload)
        expected_hash = compute_event_hash(
            prev_hash=prev_hash,
            event_type=event_type,
            payload_hash=expected_payload_hash,
            index=i,
        )
        stored_hash = str(row.get("event_hash", ""))
        stored_prev = str(row.get("prev_hash", ""))
        if stored_prev != prev_hash or stored_hash != expected_hash:
            reason = f"step {i}: prev_hash break" if stored_prev != prev_hash else f"step {i}: event_hash mismatch"
            return JournalVerifyResult(
                chain_consistent=False,
                coverage=JournalCoverageStatus.COMPLETE,
                identity=JournalIdentityStatus.UNVERIFIABLE,
                count=len(events),
                divergent_index=i,
                expected_hash=expected_hash,
                actual_hash=stored_hash,
                head=prev_hash,
                errors=[reason],
            )
        prev_hash = stored_hash

    return JournalVerifyResult(
        chain_consistent=True,
        coverage=JournalCoverageStatus.COMPLETE,
        identity=JournalIdentityStatus.UNVERIFIABLE,
        count=len(events),
        head=prev_hash,
    )


#: Event type recorded for a resolved dispatch knob selection (#2519). Folding
#: the selection into the Merkle chain means replaying a run with a different
#: knob assignment surfaces as hash divergence at a precise step index rather
#: than an unexplained cost delta.
DISPATCH_KNOB_SELECTION_EVENT = "dispatch_knob_selection"


def record_dispatch_knob_selection(
    journal: EventJournal,
    *,
    task_id: str,
    run_id: str,
    selection_hash: str,
    effort: str,
    lane: str,
    cache_strategy: str,
    rate_multiplier: float,
    resolved: bool,
    reason: str,
) -> None:
    """Append a resolved dispatch knob selection to the run journal (#2519).

    Recorded as a Merkle-chained event so the journal head covers the effort,
    lane, and cache strategy a dispatch executed with. Two operators who resolve
    identical knobs chain to the same head; a forced knob change during replay
    is reported by :meth:`EventJournal.verify` as divergence at the exact step
    index, not passed off with a different cost.

    The parameters are primitives (never the cost-layer ``KnobSelection`` type)
    so this replay module keeps no dependency on the cost package -- the caller
    projects its sealed selection onto these fields.
    """
    journal.record(
        DISPATCH_KNOB_SELECTION_EVENT,
        task_id=task_id,
        knob_run_id=run_id,
        selection_hash=selection_hash,
        effort=effort,
        lane=lane,
        cache_strategy=cache_strategy,
        rate_multiplier=round(rate_multiplier, 6),
        resolved=resolved,
        reason=reason,
    )


def seal_journal_into_spine(
    journal: EventJournal,
    *,
    lineage_root: Path,
    hmac_key: bytes,
    actor: str,
    model: str = "",
) -> str | None:
    """Record the journal's head hash into the run's lineage spine.

    Wires the replay identity (the Merkle head) into the f01 lineage
    spine at run finalization so the run's artifact provenance and its
    replay identity share one root (AC5). The head is carried as the
    spine entry's ``step_id`` and the journal file is the recorded
    artifact, so a verifier holding the spine can pin the exact replay
    identity of the run.

    Fail-closed with the spine gate: when ``BERNSTEIN_LINEAGE_ENABLED``
    is disabled this is a no-op returning ``None``.

    Args:
        journal: The finalized run journal.
        lineage_root: ``.sdd/lineage`` root; per-run dirs live beneath it.
        hmac_key: Audit-chain HMAC key used to tag the spine entry.
        actor: Producing agent / orchestrator identifier.
        model: Optional model string recorded for provenance.

    Returns:
        The spine entry hash, or ``None`` when lineage is disabled or the
        journal is empty.
    """
    from bernstein.adapters.base import record_artifact_write
    from bernstein.core.lineage.spine import JOURNAL_SEAL_STEP_PREFIX

    head = journal.head()
    if not head:
        return None
    try:
        content = journal.path.read_bytes() if journal.path.exists() else head.encode("utf-8")
    except OSError:
        content = head.encode("utf-8")
    artifact_path = f".sdd/runs/{journal.run_id}/{JOURNAL_FILENAME}"
    return record_artifact_write(
        artifact_path=artifact_path,
        content=content,
        actor=actor,
        step_id=f"{JOURNAL_SEAL_STEP_PREFIX}{head}",
        model=model,
        lineage_root=lineage_root,
        run_id=journal.run_id,
        hmac_key=hmac_key,
    )


def rebuild_state(path: Path, *, from_step: int) -> dict[str, Any]:
    """Rebuild deterministic run state by walking the journal to ``from_step``.

    Replays events ``[0, from_step)`` into a canonical state projection.
    Two independent invocations over the same journal produce byte-identical
    state, so ``bernstein replay --from-step N`` is reproducible (AC4).

    Args:
        path: Path to a ``journal.jsonl`` file.
        from_step: Exclusive upper bound (number of steps to replay). A
            value at or beyond the journal length replays everything.

    Returns:
        A state dict with a stable ``head_hash`` over the replayed prefix,
        the replayed ``step_count``, and the ordered list of replayed
        event types.
    """
    events = load_events(path).events
    upper = max(0, min(from_step, len(events)))
    prefix = events[:upper]

    prev_hash = _GENESIS_HASH
    events_seen: list[str] = []
    for i, row in enumerate(prefix):
        event_type = str(row.get("event", ""))
        payload = {k: v for k, v in row.items() if k not in _NON_DETERMINISTIC_FIELDS}
        p_hash = _payload_hash(event_type, payload)
        prev_hash = compute_event_hash(
            prev_hash=prev_hash,
            event_type=event_type,
            payload_hash=p_hash,
            index=i,
        )
        events_seen.append(event_type)

    return {
        "step_count": len(prefix),
        "head_hash": prev_hash,
        "events": events_seen,
    }


__all__ = [
    "DISPATCH_KNOB_SELECTION_EVENT",
    "JOURNAL_FILENAME",
    "RETENTION_ENV_VAR",
    "EventJournal",
    "JournalCoverageStatus",
    "JournalIdentityStatus",
    "JournalLoadResult",
    "JournalParseError",
    "JournalPathError",
    "JournalSeal",
    "JournalVerifyResult",
    "compute_event_hash",
    "contained_run_journal",
    "load_events",
    "rebuild_state",
    "record_dispatch_knob_selection",
    "seal_journal_into_spine",
    "verify_events",
    "verify_journal",
]
