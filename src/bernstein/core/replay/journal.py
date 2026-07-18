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
hashes regardless of timing. The head hash is the run identity: replay
divergence surfaces as a hash mismatch at a precise step index rather
than a silent drift.

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
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.security.path_containment import PathContainmentError, contained_path

if TYPE_CHECKING:
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
class JournalVerifyResult:
    """Outcome of :meth:`EventJournal.verify`.

    Attributes:
        ok: ``True`` only when the whole chain recomputes cleanly.
        count: Number of rows walked.
        divergent_index: 0-based index of the first row whose stored hash
            does not match the recomputed hash (or a broken ``prev_hash``
            link), or ``None`` when the chain is intact.
        expected_hash: Recomputed hash at :attr:`divergent_index`.
        actual_hash: Stored hash at :attr:`divergent_index`.
        errors: Human-readable divergence explanations.
    """

    ok: bool
    count: int
    divergent_index: int | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None
    errors: list[str] = field(default_factory=list[str])


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
        self._lock = threading.Lock()
        self._index = 0
        self._head = _GENESIS_HASH
        self._start_ts: float = time.time()
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
        events = load_events(journal.path)
        if not events:
            return journal
        result = verify_journal(journal.path)
        if not result.ok:
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
        """Return the current Merkle head hash (the run identity)."""
        return self._head

    def fingerprint(self) -> str:
        """Return the run fingerprint - the Merkle head.

        Aliased to :meth:`head` so callers that used
        ``RunRecorder.fingerprint`` keep a stable API while the value is
        now the chain head rather than a whole-file rehash.
        """
        return self._head

    def record(self, event: str, **data: Any) -> None:
        """Append one Merkle-chained event to the journal.

        Args:
            event: Event type (e.g. ``"task_claimed"``).
            **data: Arbitrary decision payload. Wall-clock fields are
                excluded from the hash but kept on the row for operators.
        """
        with self._lock:
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
                return
            self._index = index + 1
            self._head = e_hash

    def event_count(self) -> int:
        """Return the number of events recorded so far."""
        if not self._path.exists():
            return 0
        try:
            with self._path.open(encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def verify(self) -> JournalVerifyResult:
        """Recompute the whole chain and report the first divergent step.

        Walks every row, recomputing ``payload_hash`` and ``event_hash``
        from the on-disk payload and checking the ``prev_hash`` link. The
        first row whose recomputed hash differs from its stored hash (or
        whose ``prev_hash`` breaks the chain) is reported by index, so an
        injected non-deterministic result surfaces as a hash mismatch at a
        precise step rather than a silent drift.
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


def load_events(path: Path) -> list[dict[str, Any]]:
    """Load all events from a journal JSONL file in append order.

    Malformed lines are skipped so a partial trailing write cannot wedge
    a reader.
    """
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def verify_journal(path: Path) -> JournalVerifyResult:
    """Recompute a journal's chain and locate the first divergent step.

    Args:
        path: Path to a ``journal.jsonl`` file.

    Returns:
        A :class:`JournalVerifyResult`. An empty or missing file is
        reported as ``ok`` with ``count == 0``.
    """
    events = load_events(path)
    if not events:
        return JournalVerifyResult(ok=True, count=0)

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
                ok=False,
                count=len(events),
                divergent_index=i,
                expected_hash=expected_hash,
                actual_hash=stored_hash,
                errors=[reason],
            )
        prev_hash = stored_hash

    return JournalVerifyResult(ok=True, count=len(events))


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
        step_id=f"replay-journal-head:{head}",
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
    events = load_events(path)
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
    "JournalPathError",
    "JournalVerifyResult",
    "compute_event_hash",
    "load_events",
    "rebuild_state",
    "record_dispatch_knob_selection",
    "seal_journal_into_spine",
    "verify_journal",
]
